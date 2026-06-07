from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import signal
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer

from vela import __version__
from vela.agent.auth import (
    DEFAULT_AGENT_TOKEN_BYTES,
    MIN_AGENT_TOKEN_BYTES,
    AgentTokenError,
    configured_agent_token,
    default_agent_token_file,
    generate_agent_token,
    install_agent_token,
)
from vela.agent.local import LocalAgent, TargetCallError
from vela.config.schema import ModelConfig, RuntimeKind
from vela.config.targets import (
    TargetConfig,
    TargetsRegistry,
    TransportKind,
    load_targets_file,
    remove_target_file,
    upsert_target_file,
)
from vela.engine.phases import Phase
from vela.remediation import remediation_for_error
from vela.transport.client import TargetClient
from vela.transport.factory import target_client_for_config
from vela.transport.ssh_bootstrap import DEFAULT_AGENT_INSTALL_SPEC, install_ssh_agent
from vela.transport.ssh_discovery import discover_ssh_agent_command
from vela.transport.ssh_setup import setup_ssh_key
from vela.tui.app import VelaApp

app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    help="Open the Vela TUI for launching and monitoring vLLM servers.",
)
agent_app = typer.Typer(help="Run or connect to the local Vela agent.")
build_app = typer.Typer(help="Manage target-local vLLM builds.")
config_app = typer.Typer(help="Move and lint target-local deployment configs.")
deploy_app = typer.Typer(help="Create and manage target-local deployments.")
model_app = typer.Typer(help="Manage target-local model metadata.")
targets_app = typer.Typer(help="Manage controller target registry.")
app.add_typer(agent_app, name="agent")
app.add_typer(build_app, name="build")
app.add_typer(config_app, name="config")
app.add_typer(deploy_app, name="deploy")
app.add_typer(model_app, name="model")
app.add_typer(targets_app, name="targets")

BUILD_INSPECT_FIELDS = (
    "build_id",
    "label",
    "status",
    "install",
    "resolved",
    "paths",
    "created_at",
    "last_used_at",
    "notes",
)

BUILD_DOCTOR_METHODS = ("pip", "nightly", "commit", "git", "wheel", "adopt")

MODEL_INSPECT_FIELDS = (
    "entry_id",
    "display_name",
    "source",
    "repo_id",
    "revision",
    "commit_sha",
    "local_path",
    "url",
    "quant_format",
    "tokenizer",
    "files",
    "size_bytes",
    "cache_state",
    "gated",
    "token_required",
    "allow_patterns",
    "ignore_patterns",
    "created_at",
    "last_used_at",
    "notes",
)


@app.callback(invoke_without_command=True)
def interactive(
    ctx: typer.Context,
    version_requested: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show Vela version and exit.",
            is_eager=True,
        ),
    ] = False,
    configs_dir: Annotated[
        Path | None, typer.Option("--configs-dir", help="Config directory override.")
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    debug: Annotated[
        bool,
        typer.Option("--debug", help="Enable structured debug log and Textual devtools."),
    ] = False,
    debug_log: Annotated[
        Path | None,
        typer.Option("--debug-log", help="Debug JSONL path used with --debug."),
    ] = None,
) -> None:
    if version_requested:
        typer.echo(__version__)
        raise typer.Exit()
    if debug or debug_log is not None:
        _enable_textual_debug_features()
    if ctx.invoked_subcommand is None:
        _run_tui(configs_dir=configs_dir, target=target, debug=debug, debug_log=debug_log)


@app.command("tui")
def tui(
    configs_dir: Annotated[
        Path | None, typer.Option("--configs-dir", help="Config directory override.")
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    debug: Annotated[
        bool,
        typer.Option("--debug", help="Enable structured debug log and Textual devtools."),
    ] = False,
    debug_log: Annotated[
        Path | None,
        typer.Option("--debug-log", help="Debug JSONL path used with --debug."),
    ] = None,
) -> None:
    """Open the Vela TUI explicitly."""
    _run_tui(configs_dir=configs_dir, target=target, debug=debug, debug_log=debug_log)


def _run_tui(
    *,
    configs_dir: Path | None,
    target: str,
    debug: bool,
    debug_log: Path | None,
) -> None:
    if debug or debug_log is not None:
        _enable_textual_debug_features()
    VelaApp(
        configs_dir=configs_dir,
        debug_log_path=debug_log or (_default_debug_log_path() if debug else None),
        target_name=target,
    ).run()


def _enable_textual_debug_features() -> None:
    features = {
        feature.strip()
        for feature in os.environ.get("TEXTUAL", "").split(",")
        if feature.strip()
    }
    features.update({"debug", "devtools"})
    os.environ["TEXTUAL"] = ",".join(sorted(features))


def _default_debug_log_path() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "vela" / "debug.jsonl"


@app.command("doctor")
def doctor(
    target: Annotated[
        str | None,
        typer.Option("--target", help="Target name to inspect in addition to controller."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable setup checks."),
    ] = False,
) -> None:
    payload = _doctor_payload(target_name=target)
    if json_output:
        _echo_json(payload)
        return
    for check in payload["checks"]:
        status = "ok" if check["ok"] else "warn"
        typer.echo(f"{status}\t{check['name']}\t{check['detail']}")
        if check.get("remediation"):
            typer.echo(f"fix\t{check['remediation']}")
    for step in payload["next_steps"]:
        typer.echo(f"next\t{step}")


@targets_app.command("list")
def targets_list() -> None:
    registry = _load_targets_or_exit()
    for target in registry.targets:
        typer.echo(f"{target.name}\t{target.transport.value}\t{target.host or '-'}")


@targets_app.command("add")
def targets_add(
    name: Annotated[str, typer.Argument(help="Target name to add or update.")],
    transport: Annotated[
        TransportKind,
        typer.Option("--transport", help="Target transport kind."),
    ] = TransportKind.SSH,
    host: Annotated[str | None, typer.Option("--host", help="SSH host.")] = None,
    ssh_key: Annotated[
        Path | None,
        typer.Option("--ssh-key", help="SSH private key path for this target."),
    ] = None,
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Remote working directory."),
    ] = None,
    venv: Annotated[
        Path | None,
        typer.Option("--venv", help="Remote virtualenv path."),
    ] = None,
    agent_command: Annotated[
        str | None,
        typer.Option("--agent-command", help="Remote agent command, shell-split safely."),
    ] = None,
    ssh_opts_env: Annotated[
        str | None,
        typer.Option("--ssh-opts-env", help="Environment variable with SSH options."),
    ] = None,
) -> None:
    try:
        target = TargetConfig(
            name=name,
            transport=transport,
            host=host,
            ssh_key=ssh_key,
            workdir=workdir,
            venv=venv,
            agent_command=_agent_command_argv(agent_command),
            ssh_opts_env=ssh_opts_env,
        )
        target = _discover_agent_command_for_target_or_exit(target)
        upsert_target_file(target)
    except ValueError as exc:
        typer.echo(f"ERROR: Unable to add target: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"added target {name}")


@targets_app.command("bootstrap")
def targets_bootstrap(
    name: Annotated[str, typer.Argument(help="Target name to add or update.")],
    host: Annotated[str, typer.Option("--host", help="SSH host, e.g. user@host.")],
    ssh_key: Annotated[
        Path | None,
        typer.Option("--ssh-key", help="SSH private key path for this target."),
    ] = None,
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Remote working directory."),
    ] = None,
    venv: Annotated[
        Path | None,
        typer.Option("--venv", help="Remote virtualenv path."),
    ] = None,
    agent_command: Annotated[
        str | None,
        typer.Option("--agent-command", help="Remote agent command, shell-split safely."),
    ] = None,
    ssh_opts_env: Annotated[
        str | None,
        typer.Option("--ssh-opts-env", help="Environment variable with SSH options."),
    ] = None,
    install: Annotated[
        bool,
        typer.Option("--install", help="Install the Vela agent into the managed target venv."),
    ] = False,
    install_spec: Annotated[
        str,
        typer.Option(
            "--install-spec",
            help="Python package spec used by --install.",
        ),
    ] = DEFAULT_AGENT_INSTALL_SPEC,
    build: Annotated[
        str | None,
        typer.Option("--build", help="Create a default pip build from this package spec."),
    ] = None,
) -> None:
    try:
        target = TargetConfig(
            name=name,
            transport=TransportKind.SSH,
            host=host,
            ssh_key=ssh_key,
            workdir=workdir,
            venv=venv,
            agent_command=_agent_command_argv(agent_command),
            ssh_opts_env=ssh_opts_env,
        )
        target, agent_status = _bootstrap_discover_or_install_agent(
            target,
            install=install,
            install_spec=install_spec,
        )
        path = upsert_target_file(target)
    except ValueError as exc:
        typer.echo(f"ERROR: Unable to bootstrap target: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo("OK\tssh\treachable")
    typer.echo(f"OK\tagent\t{agent_status}")
    typer.echo(f"OK\ttarget\twrote {path}")
    try:
        handshake = _target_call(_target_client_for_config_or_exit(target), "handshake")
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, target_name=target.name)
    typer.echo(
        f"OK\thandshake\tagent={handshake.get('agent_version', 'unknown')}\t"
        f"protocol={handshake.get('protocol_version', 'unknown')}"
    )
    typer.echo(f"bootstrapped target {name}\t{path}")
    if build is not None:
        build_params = {
            "job_id": uuid.uuid4().hex,
            "method": "pip",
            "spec": build,
            "label": f"{name}-default",
        }
        raise typer.Exit(
            asyncio.run(
                _create_build_cli(
                    _target_client_for_config_or_exit(target),
                    build_params,
                    target_name=name,
                )
            )
        )
    typer.echo(f"next\tvela targets test {name}")


@targets_app.command("remove")
def targets_remove(
    name: Annotated[str, typer.Argument(help="Target name to remove.")],
) -> None:
    try:
        remove_target_file(name)
    except ValueError as exc:
        typer.echo(f"ERROR: Unable to remove target: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"removed target {name}")


@targets_app.command("test")
def targets_test(
    name: Annotated[str, typer.Argument(help="Target name to test.")] = "local",
) -> None:
    target = _target_config_for_name_or_exit(name)
    target = _discover_agent_command_for_target_or_exit(target, persist=True)
    client = _target_client_for_config_or_exit(target)
    try:
        handshake = _target_call(client, "handshake")
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, target_name=target.name)
    typer.echo(
        f"{name}\tok\t"
        f"agent={handshake.get('agent_version', 'unknown')}\t"
        f"protocol={handshake.get('protocol_version', 'unknown')}"
    )
    try:
        report = _target_call(_target_client_for_config_or_exit(target), "diagnose")
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, target_name=target.name)
    for line in _target_report_lines(report):
        typer.echo(line)


@targets_app.command("setup-ssh")
def targets_setup_ssh(
    name: Annotated[str, typer.Argument(help="Target name to configure.")],
    identity_file: Annotated[
        Path | None,
        typer.Option("--identity", "-i", help="SSH public/private key path for ssh-copy-id."),
    ] = None,
) -> None:
    target = _target_config_for_name_or_exit(name)
    try:
        result = setup_ssh_key(target, identity_file=identity_file)
    except ValueError as exc:
        typer.echo(f"ERROR: Unable to set up SSH: {exc}", err=True)
        raise typer.Exit(2) from exc
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, target_name=target.name)
    if result.stdout.strip():
        typer.echo(result.stdout.strip())
    typer.echo(f"setup ssh\t{name}\t{target.host}")


@build_app.command("list")
def build_list(
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable build list."),
    ] = False,
) -> None:
    result = _agent_call("list_builds", target_name=target)
    if json_output:
        _echo_json(result)
        return
    for build in result.get("builds", []):
        marker = "*" if build.get("default") else " "
        typer.echo(
            "\t".join(
                [
                    marker,
                    str(build.get("build_id") or ""),
                    str(build.get("label") or ""),
                    str(build.get("status") or "unknown"),
                ]
            )
        )
    for skipped in result.get("skipped", []):
        typer.echo(
            f"SKIPPED {skipped.get('build_id', '')}\t{skipped.get('reason', 'unknown')}"
        )


@build_app.command("doctor")
def build_doctor(
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable build readiness."),
    ] = False,
) -> None:
    checks: list[dict[str, Any]] = []
    uv_available: bool | None = None
    for method in BUILD_DOCTOR_METHODS:
        try:
            result = _agent_call(
                "check_build_prerequisites",
                {"method": method},
                target_name=target,
            )
        except TargetCallError as exc:
            remediation = remediation_for_error(
                exc.code,
                target_name=target,
                details=exc.details,
            )
            checks.append(
                {
                    "method": method,
                    "available": False,
                    "reason": exc.details.get("reason") or exc.code,
                    "message": exc.message,
                    "remediation": remediation.fix if remediation is not None else None,
                }
            )
            if exc.details.get("reason") == "uv-required":
                uv_available = False
            continue
        if "uv_available" in result:
            uv_available = bool(result["uv_available"])
        checks.append(
            {
                "method": method,
                "available": True,
                "uv_available": bool(result.get("uv_available")),
            }
        )
    payload = {
        "target": target,
        "uv_available": bool(uv_available),
        "methods": checks,
    }
    if json_output:
        _echo_json(payload)
        return
    typer.echo(f"build doctor\t{target}")
    typer.echo(f"uv\t{'available' if payload['uv_available'] else 'missing'}")
    for check in checks:
        method = str(check["method"])
        if check["available"]:
            typer.echo(f"{method}\tavailable")
            continue
        typer.echo(f"{method}\tblocked\t{check['reason']}")
        remediation = check.get("remediation")
        if remediation:
            typer.echo(str(remediation))


@build_app.command("add")
def build_add(
    method: Annotated[
        str,
        typer.Option(
            "--method",
            help=(
                "Build install method; nightly/commit require uv on the target, "
                "while pip/wheel/git can fall back to pip."
            ),
        ),
    ],
    label: Annotated[str | None, typer.Option("--label", help="Build label.")] = None,
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="Nightly or wheel CUDA channel."),
    ] = None,
    python_version: Annotated[
        str | None,
        typer.Option("--python", help="Requested Python version."),
    ] = None,
    spec: Annotated[str | None, typer.Option("--spec", help="Pip package spec.")] = None,
    commit: Annotated[str | None, typer.Option("--commit", help="vLLM commit sha.")] = None,
    url: Annotated[str | None, typer.Option("--url", help="Git repository URL.")] = None,
    ref: Annotated[str | None, typer.Option("--ref", help="Git ref.")] = None,
    path: Annotated[Path | None, typer.Option("--path", help="Wheel or venv path.")] = None,
    precompiled: Annotated[
        bool,
        typer.Option("--precompiled", help="Use precompiled vLLM extensions for git builds."),
    ] = False,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="Build environment override KEY=VALUE."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
) -> None:
    client = _target_client_for_name_or_exit(target)
    params: dict[str, Any] = {
        "job_id": uuid.uuid4().hex,
        "method": method,
    }
    for key, value in {
        "label": label,
        "channel": channel,
        "python": python_version,
        "spec": spec,
        "commit": commit,
        "url": url,
        "ref": ref,
        "path": str(path) if path is not None else None,
        "precompiled": "true" if precompiled else None,
    }.items():
        if value is not None:
            params[key] = value
    if env:
        params["env"] = list(env)
    raise typer.Exit(asyncio.run(_create_build_cli(client, params, target_name=target)))


@build_app.command("inspect")
def build_inspect(
    build: Annotated[str, typer.Argument(help="Build id or label to inspect.")],
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable build detail."),
    ] = False,
) -> None:
    try:
        result = _agent_call(
            "inspect_build",
            {"build": build},
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, target_name=target)
    if json_output:
        _echo_json(result)
        return
    manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
    for field in BUILD_INSPECT_FIELDS:
        if field not in manifest:
            continue
        value = manifest.get(field)
        if value is None or value == "":
            continue
        typer.echo(f"{field}\t{_format_inspect_value(value)}")


@build_app.command("adopt")
def build_adopt(
    venv_path: Annotated[Path, typer.Argument(help="External vLLM virtualenv to adopt.")],
    build_id: Annotated[
        str | None,
        typer.Option("--build-id", help="Deprecated; ignored. Build ids are minted."),
    ] = None,
    label: Annotated[str | None, typer.Option("--label", help="Build label.")] = None,
    vllm_version: Annotated[
        str | None,
        typer.Option("--vllm-version", help="Resolved vLLM version."),
    ] = None,
    vllm_version_profile: Annotated[
        str | None,
        typer.Option("--vllm-version-profile", help="vLLM version profile label."),
    ] = None,
    notes: Annotated[str | None, typer.Option("--notes", help="Operator notes.")] = None,
    copy: Annotated[
        bool,
        typer.Option("--copy", help="Copy the external venv into the build directory."),
    ] = False,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable adoption result."),
    ] = False,
) -> None:
    del build_id
    params = _agent_params(
        label=label,
        venv_path=venv_path,
        vllm_version=vllm_version,
        vllm_version_profile=vllm_version_profile,
        notes=notes,
        copy="true" if copy else None,
    )
    try:
        result = _agent_call("adopt_build", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    typer.echo(
        f"adopted build\t{result.get('build_id', '')}\t{result.get('label', '')}"
    )


@build_app.command("select")
def build_select(
    build: Annotated[str, typer.Argument(help="Build id or label to make active.")],
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable selection result."),
    ] = False,
) -> None:
    try:
        result = _agent_call(
            "select_build",
            {"build": build},
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    typer.echo(
        f"selected build\t{result.get('build_id', '')}\t{result.get('label', '')}"
    )


@build_app.command("verify")
def build_verify(
    build: Annotated[str, typer.Argument(help="Build id or label to verify.")],
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable verification result."),
    ] = False,
) -> None:
    try:
        result = _agent_call(
            "verify_build",
            {"build": build},
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    verdict = "OK" if result.get("ok") else "FAIL"
    typer.echo(
        "\t".join(
            [
                verdict,
                str(result.get("build_id") or build),
                str(result.get("status") or "unknown"),
                str(result.get("detail") or ""),
            ]
        )
    )


@build_app.command("repair")
def build_repair(
    build: Annotated[str, typer.Argument(help="Build id or label to repair.")],
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable repair result."),
    ] = False,
) -> None:
    try:
        result = _agent_call(
            "repair_build",
            {"build": build},
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    verdict = "OK" if result.get("ok") else "FAIL"
    typer.echo(
        "\t".join(
            [
                verdict,
                str(result.get("build_id") or build),
                str(result.get("status") or "unknown"),
                str(result.get("detail") or ""),
            ]
        )
    )


@build_app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def build_run(
    ctx: typer.Context,
    build: Annotated[str, typer.Argument(help="Build id or label to run.")],
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
) -> None:
    argv = list(ctx.args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        typer.echo("ERROR: provide the build command after --", err=True)
        raise typer.Exit(2)
    client = _target_client_for_name_or_exit(target)
    params: dict[str, object] = {
        "job_id": uuid.uuid4().hex,
        "build": build,
        "argv": argv,
    }
    raise typer.Exit(asyncio.run(_run_agent_job_cli(client, "run_build", params)))


@build_app.command("remove")
def build_remove(
    build: Annotated[str, typer.Argument(help="Build id or label to remove.")],
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Config directory for pin checks."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm removing the agent-owned build directory."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable removal result."),
    ] = False,
) -> None:
    if not yes:
        typer.echo("ERROR: use --yes to remove a build", err=True)
        raise typer.Exit(2)
    try:
        result = _agent_call(
            "remove_build",
            _agent_params(build=build, configs_dir=configs_dir),
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    typer.echo(
        f"removed build\t{result.get('build_id', '')}\t{result.get('label', '')}"
    )


@model_app.command("list")
def model_list(
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    cached_only: Annotated[
        bool,
        typer.Option("--cached-only", help="Show only cached models."),
    ] = False,
    pinned_only: Annotated[
        bool,
        typer.Option("--pinned-only", help="Show only registry-pinned models."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable model list."),
    ] = False,
) -> None:
    params = _agent_params(
        cached_only="true" if cached_only else None,
        pinned_only="true" if pinned_only else None,
    )
    result = _agent_call("list_models", params or None, target_name=target)
    if json_output:
        _echo_json(result)
        return
    for model in result.get("models", []):
        typer.echo(
            "\t".join(
                [
                    str(model.get("entry_id") or ""),
                    str(model.get("display_name") or ""),
                    str(model.get("source") or ""),
                    str(model.get("cache_state") or "unknown"),
                ]
            )
        )
    for skipped in result.get("skipped", []):
        typer.echo(
            f"SKIPPED {skipped.get('entry_id', '')}\t{skipped.get('reason', 'unknown')}"
        )


@model_app.command("refresh")
def model_refresh(
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable refresh result."),
    ] = False,
) -> None:
    try:
        result = _agent_call("refresh_models", target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    typer.echo(f"refreshed models\t{result.get('refreshed', 0)}")
    for model in result.get("models", []):
        typer.echo(
            "\t".join(
                [
                    str(model.get("entry_id") or ""),
                    str(model.get("display_name") or ""),
                    str(model.get("source") or ""),
                    str(model.get("cache_state") or "unknown"),
                ]
            )
        )
    for skipped in result.get("skipped", []):
        typer.echo(
            f"SKIPPED {skipped.get('entry_id', '')}\t{skipped.get('reason', 'unknown')}"
        )


@model_app.command("inspect")
def model_inspect(
    model_ref: Annotated[str, typer.Argument(help="Model entry id or display name.")],
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable model detail."),
    ] = False,
) -> None:
    try:
        result = _agent_call(
            "inspect_model",
            {"model_ref": model_ref},
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    for field in MODEL_INSPECT_FIELDS:
        if field not in entry:
            continue
        value = entry.get(field)
        if value is None or value == "":
            continue
        typer.echo(f"{field}\t{_format_model_inspect_value(value)}")


@model_app.command("adopt")
def model_adopt(
    local_path: Annotated[Path, typer.Argument(help="Local model directory to adopt.")],
    entry_id: Annotated[
        str | None,
        typer.Option("--entry-id", help="Deprecated; ignored. Entry ids are minted."),
    ] = None,
    display_name: Annotated[
        str | None,
        typer.Option("--display-name", "--name", help="Human-readable model name."),
    ] = None,
    quant_format: Annotated[
        str | None,
        typer.Option("--quant-format", help="Model quantization label."),
    ] = None,
    tokenizer: Annotated[
        str | None,
        typer.Option("--tokenizer", help="Tokenizer override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable adoption result."),
    ] = False,
) -> None:
    del entry_id
    params = _agent_params(
        display_name=display_name,
        local_path=local_path,
        quant_format=quant_format,
        tokenizer=tokenizer,
    )
    params["source"] = "local_path"
    try:
        result = _agent_call("pin_model", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    entry = result.get("entry", {})
    typer.echo(
        f"adopted model\t{entry.get('entry_id', '')}\t{entry.get('display_name', '')}"
    )


@model_app.command("pin")
@model_app.command("add")
def model_pin(
    repo_or_entry: Annotated[
        str,
        typer.Argument(
            help=(
                "Hugging Face repo id, URL display name, or legacy entry id when "
                "--repo-id is supplied."
            )
        ),
    ],
    entry_id: Annotated[
        str | None,
        typer.Option("--entry-id", help="Deprecated; ignored. Entry ids are minted."),
    ] = None,
    repo_id: Annotated[str | None, typer.Option("--repo-id", help="Hugging Face repo id.")] = None,
    display_name: Annotated[
        str | None,
        typer.Option("--display-name", "--name", help="Human-readable model name."),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option("--revision", help="Revision, branch, tag, or commit sha."),
    ] = None,
    commit_sha: Annotated[
        str | None,
        typer.Option("--commit-sha", help="Resolved immutable commit sha."),
    ] = None,
    local_path: Annotated[
        Path | None,
        typer.Option("--local-path", help="Adopt a local model directory."),
    ] = None,
    url: Annotated[str | None, typer.Option("--url", help="Remote model URL.")] = None,
    quant_format: Annotated[
        str | None,
        typer.Option("--quant-format", help="Model quantization label."),
    ] = None,
    tokenizer: Annotated[
        str | None,
        typer.Option("--tokenizer", help="Tokenizer override."),
    ] = None,
    gated: Annotated[
        bool,
        typer.Option("--gated", help="Mark model metadata as gated."),
    ] = False,
    token_required: Annotated[
        bool,
        typer.Option("--token-required", help="Mark model metadata as requiring HF_TOKEN."),
    ] = False,
    notes: Annotated[
        str | None,
        typer.Option("--notes", help="Operator notes."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable pin result."),
    ] = False,
) -> None:
    del entry_id
    if url is not None:
        params = _agent_params(
            url=url,
            display_name=display_name or repo_or_entry,
            quant_format=quant_format,
            tokenizer=tokenizer,
            gated="true" if gated else None,
            token_required="true" if token_required else None,
            notes=notes,
        )
        params["source"] = "url"
    else:
        selected_repo_id = repo_id or repo_or_entry
        selected_display_name = display_name or (
            repo_or_entry if repo_id is not None else None
        )
        params = _agent_params(
            repo_id=selected_repo_id,
            display_name=selected_display_name,
            revision=revision,
            commit_sha=commit_sha,
            local_path=local_path,
            quant_format=quant_format,
            tokenizer=tokenizer,
            gated="true" if gated else None,
            token_required="true" if token_required else None,
            notes=notes,
        )
    if local_path is not None and url is None:
        params["source"] = "local_path"
    try:
        result = _agent_call("pin_model", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    entry = result.get("entry", {})
    typer.echo(
        f"pinned model\t{entry.get('entry_id', '')}\t{entry.get('display_name', '')}"
    )


@model_app.command("verify")
def model_verify(
    model_ref: Annotated[str, typer.Argument(help="Model entry id or display name.")],
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    deep: Annotated[
        bool,
        typer.Option("--deep", help="Run deep content-hash verification."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable verification result."),
    ] = False,
) -> None:
    params = _agent_params(model_ref=model_ref, deep="true" if deep else None)
    try:
        result = _agent_call(
            "verify_model",
            params,
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    verdict = "OK" if result.get("ok") else "FAIL"
    typer.echo(
        "\t".join(
            [
                verdict,
                str(result.get("entry_id") or model_ref),
                str(result.get("cache_state") or "unknown"),
                str(result.get("detail") or ""),
            ]
        )
    )


@model_app.command("download")
def model_download(
    model_ref: Annotated[str, typer.Argument(help="Model entry id or display name.")],
    revision: Annotated[
        str | None,
        typer.Option("--revision", help="Revision override for the download job."),
    ] = None,
    allow_patterns: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Allow-pattern passed to Hugging Face download."),
    ] = None,
    ignore_patterns: Annotated[
        list[str] | None,
        typer.Option("--ignore", help="Ignore-pattern passed to Hugging Face download."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit final job result as machine-readable JSON."),
    ] = False,
) -> None:
    client = _target_client_for_name_or_exit(target)
    params: dict[str, object] = {
        "job_id": uuid.uuid4().hex,
        "model_ref": model_ref,
    }
    if revision is not None:
        params["revision"] = revision
    if allow_patterns:
        params["allow_patterns"] = list(allow_patterns)
    if ignore_patterns:
        params["ignore_patterns"] = list(ignore_patterns)
    raise typer.Exit(
        asyncio.run(
            _run_agent_job_cli(
                client,
                "download_model",
                params,
                json_output=json_output,
            )
        )
    )


@model_app.command("remove")
def model_remove(
    model_ref: Annotated[str, typer.Argument(help="Model entry id or display name.")],
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Config directory for pin checks."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm removing model metadata."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Override config pin protection."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable removal result."),
    ] = False,
) -> None:
    if not yes:
        typer.echo("ERROR: use --yes to remove model metadata", err=True)
        raise typer.Exit(2)
    try:
        result = _agent_call(
            "remove_model",
            _agent_params(
                model_ref=model_ref,
                configs_dir=configs_dir,
                force="true" if force else None,
            ),
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    entry = result.get("entry", {})
    fields = [
        "removed model",
        str(result.get("entry_id", "")),
        str(entry.get("display_name", "")),
    ]
    if result.get("removed_weights"):
        fields.append(f"freed ~{_format_bytes(result.get('expected_freed_size'))}")
    typer.echo("\t".join(fields))


@config_app.command("push")
def config_push(
    file: Annotated[Path, typer.Argument(help="Local config YAML to push to the target.")],
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Overwrite an existing target config."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable push result."),
    ] = False,
) -> None:
    try:
        yaml_text = file.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"ERROR: unable to read config file: {exc}", err=True)
        raise typer.Exit(2) from exc
    params: dict[str, Any] = {"yaml": yaml_text}
    if configs_dir is not None:
        params["configs_dir"] = str(configs_dir)
    if overwrite:
        params["overwrite"] = True
    try:
        result = _agent_call("push_config", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    _echo_warnings(result.get("warnings", []))
    typer.echo(f"pushed config\t{result.get('name', '')}\t{result.get('path', '')}")


@config_app.command("pull")
def config_pull(
    name: Annotated[str, typer.Argument(help="Target config name to pull.")],
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Local path to write the pulled YAML."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable pull result."),
    ] = False,
) -> None:
    params: dict[str, Any] = {"name": name}
    if configs_dir is not None:
        params["configs_dir"] = str(configs_dir)
    try:
        result = _agent_call("pull_config", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)
    if json_output:
        _echo_json(result)
        return
    yaml_text = str(result.get("yaml") or "")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(yaml_text, encoding="utf-8")
        typer.echo(f"pulled config\t{result.get('name', name)}\t{output}")
        return
    typer.echo(yaml_text, nl=False)


@config_app.command("lint")
def config_lint(
    file: Annotated[Path, typer.Argument(help="Local config YAML to lint.")],
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable lint result."),
    ] = False,
) -> None:
    try:
        yaml_text = file.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"ERROR: unable to read config file: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        result = _agent_call("lint_config", {"yaml": yaml_text}, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    _echo_config_lint_result(result)
    if result.get("ok") is not True:
        raise typer.Exit(1)
    typer.echo("config lint ok")


@config_app.command("edit")
def config_edit(
    name: Annotated[str, typer.Argument(help="Target config name to edit.")],
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable edit result."),
    ] = False,
) -> None:
    pull_params: dict[str, Any] = {"name": name}
    if configs_dir is not None:
        pull_params["configs_dir"] = str(configs_dir)
    try:
        pulled = _agent_call("pull_config", pull_params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)
    original_yaml = str(pulled.get("yaml") or "")
    edited_yaml = typer.edit(original_yaml, extension=".yaml")
    if edited_yaml is None:
        typer.echo("edit cancelled", err=True)
        raise typer.Exit(1)
    if edited_yaml == original_yaml:
        payload = {"name": name, "changed": False, "path": pulled.get("path")}
        if json_output:
            _echo_json(payload)
            return
        typer.echo(f"config unchanged\t{name}\t{pulled.get('path', '')}")
        return
    try:
        linted = _agent_call("lint_config", {"yaml": edited_yaml}, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)
    if linted.get("ok") is not True:
        if json_output:
            _echo_json({"name": name, "ok": False, "lint": linted})
            raise typer.Exit(2)
        _echo_config_lint_result(linted)
        raise typer.Exit(2)
    push_params: dict[str, Any] = {
        "name": name,
        "yaml": edited_yaml,
        "overwrite": True,
    }
    if configs_dir is not None:
        push_params["configs_dir"] = str(configs_dir)
    try:
        pushed = _agent_call("push_config", push_params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)
    if json_output:
        _echo_json(pushed)
        return
    _echo_warnings(pushed.get("warnings", []))
    typer.echo(f"edited config\t{pushed.get('name', name)}\t{pushed.get('path', '')}")


@deploy_app.command("create")
def deploy_create(
    name: Annotated[str, typer.Argument(help="Deployment/config name to create.")],
    model: Annotated[
        str | None,
        typer.Option("--model", help="Launch model repo id, URL, or target-local path."),
    ] = None,
    model_ref: Annotated[
        str | None,
        typer.Option("--model-ref", help="Target model registry entry/display name."),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option("--revision", help="Model revision override."),
    ] = None,
    runtime: Annotated[
        str,
        typer.Option("--runtime", help="Runtime: process, docker, build, or executable."),
    ] = "process",
    image: Annotated[
        str | None,
        typer.Option("--image", help="Pinned docker image/tag/digest for docker runtime."),
    ] = None,
    build: Annotated[
        str | None,
        typer.Option("--build", help="Target build id or label for process runtime."),
    ] = None,
    executable: Annotated[
        Path | None,
        typer.Option("--executable", help="Target-local vLLM executable path."),
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option("--preset", help="Deployment composer preset."),
    ] = None,
    port: Annotated[
        str | None,
        typer.Option("--port", help="Preferred port, or 'auto' to allocate."),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option("--host", help="Server bind host override."),
    ] = None,
    exposure: Annotated[
        str | None,
        typer.Option("--exposure", help="Exposure override: local, lan, or public."),
    ] = None,
    set_overrides: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Override a config field, e.g. engine.kv_cache_dtype=fp8.",
        ),
    ] = None,
    extra_args: Annotated[
        list[str] | None,
        typer.Option(
            "--extra-arg",
            help="Append one raw vLLM serve arg. Use --extra-arg=--flag for flag values.",
        ),
    ] = None,
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Compose, validate, and preview without saving."),
    ] = False,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Overwrite an existing config of the same name."),
    ] = False,
    smoke: Annotated[
        bool,
        typer.Option("--smoke", help="Run a bounded smoke after saving."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable compose result."),
    ] = False,
) -> None:
    if model is None and model_ref is None:
        typer.echo("ERROR: provide --model or --model-ref", err=True)
        raise typer.Exit(2)
    if smoke and json_output:
        typer.echo("ERROR: --smoke cannot be combined with --json", err=True)
        raise typer.Exit(2)
    try:
        spec = _deploy_create_spec(
            name=name,
            target=target,
            runtime=runtime,
            model=model,
            model_ref=model_ref,
            revision=revision,
            image=image,
            build=build,
            executable=executable,
            preset=preset,
            port=port,
            host=host,
            exposure=exposure,
            set_overrides=set_overrides,
            extra_args=extra_args,
            configs_dir=configs_dir,
        )
    except ValueError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc

    try:
        composed = _agent_call("compose_config", spec, target_name=target)
        config = composed["config"]
        validation = _agent_call("validate_config", {"config": config}, target_name=target)
        if not validation.get("ok"):
            _echo_deploy_validation_errors_or_exit(validation)
        preview_params: dict[str, Any] = {"config": config}
        if configs_dir is not None:
            preview_params["configs_dir"] = str(configs_dir)
        preview_result = _agent_call("preview", preview_params, target_name=target)
        preflight_result: dict[str, Any] | None = None
        saved: dict[str, Any] | None = None
        if not dry_run:
            preflight_result = _agent_call("preflight", preview_params, target_name=target)
            save_params: dict[str, Any] = {"name": name, "config": config}
            if configs_dir is not None:
                save_params["configs_dir"] = str(configs_dir)
            save_params["overwrite"] = True
            saved = _agent_call("save_config", save_params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)

    payload = {
        "config": config,
        "warnings": list(composed.get("warnings", [])),
        "derived": list(composed.get("derived", [])),
        "validation": validation,
        "preview": preview_result.get("preview", ""),
    }
    if preflight_result is not None:
        payload["preflight"] = preflight_result
    if saved is not None:
        payload["saved"] = {
            "name": saved.get("name", name),
            "path": saved.get("path"),
        }
    if json_output:
        _echo_json(payload)
        return

    _echo_warnings(payload["warnings"])
    _echo_warnings(validation.get("warnings", []))
    _echo_warnings(preview_result.get("warnings", []))
    if dry_run:
        typer.echo(f"dry-run deployment\t{name}")
    else:
        typer.echo(f"saved deployment\t{name}\t{saved.get('path') if saved else ''}")
    typer.echo(str(preview_result.get("preview", "")))

    if smoke:
        client = _target_client_for_name_or_exit(target)
        prepared = _prepare_launch_with_client_or_exit(client, name, configs_dir)
        raise typer.Exit(
            asyncio.run(_smoke_config_cli(client, prepared, name, configs_dir))
        )


@deploy_app.command("edit")
def deploy_edit(
    name: Annotated[str, typer.Argument(help="Deployment/config name to edit.")],
    port: Annotated[
        str | None,
        typer.Option("--port", help="Preferred port, or 'auto' to leave unchanged."),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option("--host", help="Server bind host override."),
    ] = None,
    exposure: Annotated[
        str | None,
        typer.Option("--exposure", help="Exposure override: local, lan, or public."),
    ] = None,
    set_overrides: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Override a config field, e.g. engine.kv_cache_dtype=fp8.",
        ),
    ] = None,
    extra_args: Annotated[
        list[str] | None,
        typer.Option(
            "--extra-arg",
            help="Append one raw vLLM serve arg. Use --extra-arg=--flag for flag values.",
        ),
    ] = None,
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate edits without saving."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable edit result."),
    ] = False,
) -> None:
    try:
        overrides = _deploy_overrides(
            port=port,
            host=host,
            exposure=exposure,
            set_overrides=set_overrides,
            extra_args=extra_args,
        )
    except ValueError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    params: dict[str, Any] = {"name": name, "overrides": overrides}
    if configs_dir is not None:
        params["configs_dir"] = str(configs_dir)
    if dry_run:
        params["dry_run"] = True
    try:
        result = _agent_call("edit_config", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)
    if json_output:
        _echo_json(result)
        return
    _echo_warnings(result.get("warnings", []))
    verb = "dry-run deployment" if dry_run else "edited deployment"
    typer.echo(f"{verb}\t{result.get('name', name)}\t{result.get('path', '')}")


@deploy_app.command("list")
def deploy_list(
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable deployment list."),
    ] = False,
) -> None:
    try:
        result = _agent_call(
            "list_configs",
            _agent_params(configs_dir=configs_dir),
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    if json_output:
        _echo_json(result)
        return
    for item in result.get("valid", []):
        typer.echo(f"{item['name']}\t{item['model']}")
    for item in result.get("invalid", []):
        typer.echo(f"INVALID {Path(item['path']).name}\t{'; '.join(item['errors'])}")


@deploy_app.command("clone")
def deploy_clone(
    src_name: Annotated[str, typer.Argument(help="Existing deployment/config name.")],
    new_name: Annotated[str, typer.Argument(help="New deployment/config name.")],
    set_overrides: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Override a config field, e.g. server.port=18005.",
        ),
    ] = None,
    extra_args: Annotated[
        list[str] | None,
        typer.Option(
            "--extra-arg",
            help="Append one raw vLLM serve arg. Use --extra-arg=--flag for flag values.",
        ),
    ] = None,
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Overwrite an existing cloned config."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable clone result."),
    ] = False,
) -> None:
    try:
        overrides = _deploy_overrides(
            port=None,
            host=None,
            exposure=None,
            set_overrides=set_overrides,
            extra_args=extra_args,
        )
    except ValueError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc
    params: dict[str, Any] = {"src_name": src_name, "new_name": new_name}
    if configs_dir is not None:
        params["configs_dir"] = str(configs_dir)
    if overrides:
        params["overrides"] = overrides
    if overwrite:
        params["overwrite"] = True
    try:
        result = _agent_call("clone_config", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=new_name)
    if json_output:
        _echo_json(result)
        return
    typer.echo(f"cloned deployment\t{result.get('name', new_name)}\t{result.get('path', '')}")


@deploy_app.command("from-wrapper")
def deploy_from_wrapper(
    src_name: Annotated[
        str,
        typer.Argument(help="Existing wrapper-based deployment/config name."),
    ],
    new_name: Annotated[
        str | None,
        typer.Argument(help="New native-docker deployment/config name."),
    ] = None,
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Emit the migrated config without saving."),
    ] = False,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Overwrite an existing migrated config."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable migration result."),
    ] = False,
) -> None:
    params: dict[str, Any] = {"src_name": src_name}
    if new_name is not None:
        params["new_name"] = new_name
    if configs_dir is not None:
        params["configs_dir"] = str(configs_dir)
    if dry_run:
        params["dry_run"] = True
    if overwrite:
        params["overwrite"] = True
    try:
        result = _agent_call("migrate_wrapper_config", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=new_name or src_name)
    if json_output:
        _echo_json(result)
        return
    _echo_warnings(result.get("warnings", []))
    verb = "dry-run wrapper migration" if dry_run else "migrated wrapper"
    typer.echo(f"{verb}\t{result.get('name', new_name or src_name)}\t{result.get('path', '')}")


@deploy_app.command("delete")
def deploy_delete(
    name: Annotated[str, typer.Argument(help="Deployment/config name to delete.")],
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm deleting the target-local config."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable delete result."),
    ] = False,
) -> None:
    if not yes:
        typer.echo("ERROR: use --yes to delete a deployment", err=True)
        raise typer.Exit(2)
    params = _agent_params(name=name, configs_dir=configs_dir)
    try:
        result = _agent_call("delete_config", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)
    if json_output:
        _echo_json(result)
        return
    typer.echo(f"deleted deployment\t{name}\t{result.get('path', '')}")


@deploy_app.command("export")
def deploy_export(
    name: Annotated[str, typer.Argument(help="Deployment/config name to export.")],
    output_path: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Target-local path to write the standalone docker script.",
        ),
    ] = None,
    configs_dir: Annotated[
        Path | None,
        typer.Option("--configs-dir", help="Target config directory override."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Overwrite an existing exported script."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable export result."),
    ] = False,
) -> None:
    params: dict[str, Any] = {"name": name}
    if configs_dir is not None:
        params["configs_dir"] = str(configs_dir)
    if output_path is not None:
        params["output_path"] = str(output_path)
    if overwrite:
        params["overwrite"] = True
    try:
        result = _agent_call("export_config", params, target_name=target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)
    if json_output:
        _echo_json(result)
        return
    _echo_warnings(result.get("warnings", []))
    if output_path is not None:
        typer.echo(f"exported deployment\t{name}\t{result.get('path', '')}")
        return
    script = str(result.get("script", ""))
    typer.echo(script, nl=not script.endswith("\n"))


@app.command("list")
def list_configs(
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
) -> None:
    result = _agent_call(
        "list_configs",
        _agent_params(configs_dir=configs_dir),
        target_name=target,
    )
    for item in result["valid"]:
        typer.echo(f"{item['name']}\t{item['model']}")
    for item in result["invalid"]:
        typer.echo(f"INVALID {Path(item['path']).name}\t{'; '.join(item['errors'])}")


@app.command("preview")
def preview(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    build_id: Annotated[
        str | None,
        typer.Option("--build-id", help="Target build id or label override."),
    ] = None,
    model_ref: Annotated[
        str | None,
        typer.Option("--model-ref", help="Target model entry/display override."),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option("--revision", help="Model revision override."),
    ] = None,
) -> None:
    try:
        result = _agent_call(
            "preview",
            _agent_params(
                name=name,
                configs_dir=configs_dir,
                build_id=build_id,
                model_ref=model_ref,
                revision=revision,
            ),
            target_name=target,
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)
    typer.echo(result["preview"])
    _echo_warnings(result.get("warnings", []))


@app.command("run")
def run_config(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
    preview_only: Annotated[
        bool, typer.Option("--preview", help="Print command instead of launching.")
    ] = False,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    build_id: Annotated[
        str | None,
        typer.Option("--build-id", help="Target build id or label override."),
    ] = None,
    model_ref: Annotated[
        str | None,
        typer.Option("--model-ref", help="Target model entry/display override."),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option("--revision", help="Model revision override."),
    ] = None,
) -> None:
    client = _target_client_for_name_or_exit(target)
    overrides = _launch_override_params(
        build_id=build_id,
        model_ref=model_ref,
        revision=revision,
    )
    if preview_only:
        try:
            result = _target_call(
                client,
                "preview",
                _agent_params(name=name, configs_dir=configs_dir, **overrides),
            )
        except TargetCallError as exc:
            _echo_target_error_or_exit(exc, fallback_name=name)
        typer.echo(result["preview"])
        _echo_warnings(result.get("warnings", []))
        return
    prepared = _prepare_launch_with_client_or_exit(
        client,
        name,
        configs_dir,
        **overrides,
    )
    cfg = ModelConfig.model_validate(prepared["config"])
    if cfg.launch.mode.value == "detached":
        asyncio.run(_run_detached_cli(client, name, configs_dir, prepared, **overrides))
        return

    raise typer.Exit(
        asyncio.run(_run_attached_cli(client, name, configs_dir, prepared, **overrides))
    )


@app.command("smoke")
def smoke_config(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    build_id: Annotated[
        str | None,
        typer.Option("--build-id", help="Target build id or label override."),
    ] = None,
    model_ref: Annotated[
        str | None,
        typer.Option("--model-ref", help="Target model entry/display override."),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option("--revision", help="Model revision override."),
    ] = None,
) -> None:
    client = _target_client_for_name_or_exit(target)
    overrides = _launch_override_params(
        build_id=build_id,
        model_ref=model_ref,
        revision=revision,
    )
    prepared = _prepare_launch_with_client_or_exit(
        client,
        name,
        configs_dir,
        **overrides,
    )
    raise typer.Exit(
        asyncio.run(_smoke_config_cli(client, prepared, name, configs_dir, **overrides))
    )


@app.command("smoke-tui")
def smoke_tui_config(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
    build_id: Annotated[
        str | None,
        typer.Option("--build-id", help="Target build id or label override."),
    ] = None,
    model_ref: Annotated[
        str | None,
        typer.Option("--model-ref", help="Target model entry/display override."),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option("--revision", help="Model revision override."),
    ] = None,
) -> None:
    client = _target_client_for_name_or_exit(target)
    overrides = _launch_override_params(
        build_id=build_id,
        model_ref=model_ref,
        revision=revision,
    )
    prepared = _prepare_launch_with_client_or_exit(
        client,
        name,
        configs_dir,
        **overrides,
    )
    cfg = ModelConfig.model_validate(prepared["config"])
    helper_overrides = _agent_params(**overrides)
    raise typer.Exit(
        asyncio.run(
            _smoke_tui_config_cli(
                cfg.name,
                configs_dir,
                target,
                **helper_overrides,
            )
        )
    )


def _deploy_create_spec(
    *,
    name: str,
    target: str,
    runtime: str,
    model: str | None,
    model_ref: str | None,
    revision: str | None,
    image: str | None,
    build: str | None,
    executable: Path | None,
    preset: str | None,
    port: str | None,
    host: str | None,
    exposure: str | None,
    set_overrides: list[str] | None,
    extra_args: list[str] | None,
    configs_dir: Path | None,
) -> dict[str, Any]:
    overrides = _deploy_overrides(
        port=port,
        host=host,
        exposure=exposure,
        set_overrides=set_overrides,
        extra_args=extra_args,
    )
    spec: dict[str, Any] = {
        "name": name,
        "target": target,
        "runtime": _deploy_runtime_spec(
            runtime,
            image=image,
            build=build,
            executable=executable,
        ),
    }
    if model is not None:
        spec["model"] = model
    if model_ref is not None:
        spec["model_ref"] = model_ref
    if revision is not None:
        spec["revision"] = revision
    if preset is not None:
        spec["preset"] = preset
    if overrides:
        spec["overrides"] = overrides
    if configs_dir is not None:
        spec["configs_dir"] = str(configs_dir)
    return spec


def _deploy_runtime_spec(
    runtime: str,
    *,
    image: str | None,
    build: str | None,
    executable: Path | None,
) -> str | dict[str, str]:
    kind = runtime.strip().lower()
    if kind == "docker":
        if build is not None or executable is not None:
            raise ValueError("--runtime docker cannot be combined with --build or --executable")
        payload = {"kind": "docker"}
        if image is not None:
            payload["image"] = image
        return payload
    if image is not None:
        raise ValueError("--image requires --runtime docker")
    if executable is not None:
        if build is not None:
            raise ValueError("--build cannot be combined with --executable")
        return {"kind": "executable", "executable": str(executable)}
    if build is not None:
        return {"kind": "build", "build": build}
    if kind in {"process", "build", "create_build", "adopt", "executable"}:
        return kind
    raise ValueError(f"unsupported runtime: {runtime}")


def _deploy_overrides(
    *,
    port: str | None,
    host: str | None,
    exposure: str | None,
    set_overrides: list[str] | None,
    extra_args: list[str] | None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if port is not None and port.lower() != "auto":
        try:
            parsed_port = int(port)
        except ValueError as exc:
            raise ValueError(f"--port must be an integer or 'auto': {port}") from exc
        _deploy_put_override(overrides, ["server", "port"], parsed_port)
    if host is not None:
        _deploy_put_override(overrides, ["server", "host"], host)
    if exposure is not None:
        _deploy_put_override(overrides, ["server", "exposure"], exposure)
    for item in set_overrides or []:
        field, value = _deploy_parse_set(item)
        _deploy_put_override(overrides, field, value)
    if extra_args:
        overrides["extra_args"] = list(extra_args)
    return overrides


def _deploy_parse_set(item: str) -> tuple[list[str], Any]:
    if "=" not in item:
        raise ValueError(f"--set requires FIELD=VALUE: {item}")
    field, raw_value = item.split("=", 1)
    parts = [part.strip() for part in field.split(".") if part.strip()]
    if len(parts) < 2:
        raise ValueError(f"--set field must include a section and key: {field}")
    if parts[0] not in {"engine", "server", "launch", "env"}:
        raise ValueError(
            "--set supports engine.*, server.*, launch.*, and env.* overrides"
        )
    return parts, _deploy_parse_value(raw_value)


def _deploy_parse_value(raw_value: str) -> Any:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def _deploy_put_override(overrides: dict[str, Any], field: list[str], value: Any) -> None:
    current = overrides.setdefault(field[0], {})
    if not isinstance(current, dict):
        raise ValueError(f"override section {field[0]} is already set")
    for part in field[1:-1]:
        nested = current.setdefault(part, {})
        if not isinstance(nested, dict):
            raise ValueError(f"override field {'.'.join(field[:-1])} is already set")
        current = nested
    current[field[-1]] = value


def _echo_deploy_validation_errors_or_exit(validation: dict[str, Any]) -> None:
    typer.echo("ERROR: composed deployment is invalid", err=True)
    for item in validation.get("errors", []):
        field = item.get("field", "config") if isinstance(item, dict) else "config"
        message = item.get("message", str(item)) if isinstance(item, dict) else str(item)
        typer.echo(f"{field}: {message}", err=True)
    raise typer.Exit(2)


def _echo_warnings(warnings) -> None:
    for warning in warnings:
        typer.echo(f"WARNING: {warning}", err=True)


def _echo_config_lint_result(result: dict[str, Any]) -> None:
    for error in result.get("errors") or []:
        if isinstance(error, dict):
            field = error.get("field", "config")
            message = error.get("message", "")
            typer.echo(f"ERROR: {field}: {message}")
        else:
            typer.echo(f"ERROR: {error}")
    _echo_warnings(result.get("warnings", []))


def _echo_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, sort_keys=True))


def _agent_command_argv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    argv = shlex.split(value)
    if not argv:
        raise ValueError("--agent-command must not be empty")
    return argv


def _doctor_payload(*, target_name: str | None = None) -> dict[str, Any]:
    checks: list[dict[str, object]] = []
    next_steps: list[str] = []
    try:
        registry = load_targets_file()
    except ValueError as exc:
        checks.append({"name": "targets", "ok": False, "detail": str(exc)})
        registry = None
    else:
        remote_targets = [target for target in registry.targets if target.name != "local"]
        checks.append(
            {
                "name": "targets",
                "ok": True,
                "detail": f"{len(remote_targets)} remote target(s) configured",
            }
        )

    try:
        token = configured_agent_token()
    except AgentTokenError as exc:
        checks.append(
            {
                "name": "agent_token",
                "ok": False,
                "detail": str(exc),
                "remediation": "run `vela agent gen-token --install`",
            }
        )
        next_steps.append("vela agent gen-token --install")
    else:
        detail = (
            "configured via VELA_AGENT_TOKEN or token file"
            if token
            else f"not installed; default path is {default_agent_token_file()}"
        )
        checks.append({"name": "agent_token", "ok": True, "detail": detail})

    if target_name is not None:
        _append_target_doctor_checks(checks, next_steps, target_name, registry)

    return {
        "ok": all(bool(check["ok"]) for check in checks),
        "target": target_name,
        "checks": checks,
        "next_steps": next_steps,
    }


def _append_target_doctor_checks(
    checks: list[dict[str, object]],
    next_steps: list[str],
    target_name: str,
    registry: TargetsRegistry | None,
) -> None:
    if registry is None:
        return
    try:
        target = registry.by_name(target_name)
    except KeyError:
        remediation = f"vela targets bootstrap {target_name} --install"
        checks.append(
            {
                "name": "target",
                "ok": False,
                "detail": f"unknown target {target_name}",
                "remediation": f"run `{remediation}`",
            }
        )
        next_steps.append(remediation)
        return
    if target.transport is TransportKind.SSH and target.agent_command is None:
        try:
            discovery = discover_ssh_agent_command(target)
        except TargetCallError as exc:
            remediation = remediation_for_error(
                exc.code,
                target_name=target_name,
                details=exc.details,
            )
            command = (
                remediation.fix.removeprefix("Fix: ").rstrip(".")
                if remediation is not None
                else f"vela targets bootstrap {target_name} --install"
            )
            checks.append(
                {
                    "name": "target_agent",
                    "ok": False,
                    "detail": exc.message,
                    "remediation": command,
                }
            )
            next_steps.append(command)
            return
        target = target.model_copy(update={"agent_command": discovery.agent_command})
        upsert_target_file(target)
    try:
        client = target_client_for_config(target)
    except ValueError as exc:
        checks.append({"name": "target_client", "ok": False, "detail": str(exc)})
        return
    try:
        report = _target_call(client, "diagnose")
    except TargetCallError as exc:
        auth_status = _target_auth_status_from_error(exc)
        remediation = remediation_for_error(
            exc.code,
            target_name=target_name,
            details=exc.details,
        )
        checks.append(
            {
                "name": "target_connection",
                "ok": False,
                "detail": exc.message,
                "remediation": remediation.fix if remediation is not None else None,
            }
        )
        if remediation is not None:
            next_steps.append(remediation.fix.removeprefix("Fix: ").rstrip("."))
        if auth_status is not None:
            command = f"vela agent gen-token --install --target {target_name}"
            checks.append(
                {
                    "name": "target_auth",
                    "ok": False,
                    "detail": auth_status,
                    "remediation": f"run `{command}`",
                }
            )
            if command not in next_steps:
                next_steps.append(command)
        return
    checks.append({"name": "target_connection", "ok": True, "detail": "agent reachable"})
    _append_target_report_checks(checks, report)


def _append_target_report_checks(
    checks: list[dict[str, object]],
    report: dict[str, Any],
) -> None:
    parts = _target_report_parts(report)
    checks.append(
        {
            "name": "target_version",
            "ok": parts["agent_version"] == __version__,
            "detail": parts["version_detail"],
        }
    )
    checks.append(
        {
            "name": "target_paths",
            "ok": True,
            "detail": parts["paths_detail"],
        }
    )
    checks.append(
        {
            "name": "target_toolchain",
            "ok": True,
            "detail": parts["toolchain_detail"],
        }
    )
    checks.append(
        {
            "name": "target_auth",
            "ok": parts["auth_status"] != "malformed-token",
            "detail": parts["auth_status"],
        }
    )


def _target_report_lines(report: dict[str, Any]) -> list[str]:
    parts = _target_report_parts(report)
    version_status = "ok" if parts["agent_version"] == __version__ else "mismatch"
    return [
        f"version\t{version_status}\t{parts['version_detail']}",
        f"host\t{parts['host_detail']}",
        f"paths\t{parts['paths_detail']}",
        f"toolchain\t{parts['toolchain_detail']}",
        f"auth\t{parts['auth_status']}",
    ]


def _target_report_parts(report: dict[str, Any]) -> dict[str, str]:
    host = report.get("host") if isinstance(report.get("host"), dict) else {}
    paths = report.get("paths") if isinstance(report.get("paths"), dict) else {}
    toolchain = (
        report.get("toolchain") if isinstance(report.get("toolchain"), dict) else {}
    )
    auth = report.get("auth") if isinstance(report.get("auth"), dict) else {}
    agent_version = str(host.get("vela_version") or "unknown")
    return {
        "agent_version": agent_version,
        "version_detail": f"agent={agent_version} controller={__version__}",
        "host_detail": (
            f"hostname={host.get('hostname', 'unknown')} "
            f"platform={host.get('platform', 'unknown')}"
        ),
        "paths_detail": (
            f"config={paths.get('config_dir', 'unknown')} "
            f"runs={paths.get('runs_dir', 'unknown')} "
            f"builds={paths.get('builds_dir', 'unknown')} "
            f"models={paths.get('models_registry', 'unknown')} "
            f"socket={paths.get('socket_path', 'unknown')}"
        ),
        "toolchain_detail": (
            f"python={toolchain.get('python', 'unknown')} "
            f"uv={'yes' if toolchain.get('uv_available') else 'no'} "
            f"driver={host.get('driver', 'unknown')}"
        ),
        "auth_status": str(auth.get("status") or "unknown"),
    }


def _target_auth_status_from_error(exc: TargetCallError) -> str | None:
    if exc.code != "agent-auth-required":
        return None
    reason = str(exc.details.get("reason") or "")
    if reason == "capability-token-misconfigured":
        return "malformed-token"
    try:
        token = configured_agent_token()
    except AgentTokenError:
        return "malformed-token"
    if token is None:
        return "required+missing"
    return "mismatch"


def _format_model_inspect_value(value: object) -> str:
    return _format_inspect_value(value)


def _format_inspect_value(value: object) -> str:
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _prepare_launch_with_client_or_exit(
    client: TargetClient,
    name: str,
    configs_dir: Path | None,
    *,
    build_id: str | None = None,
    model_ref: str | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    try:
        return _target_call(
            client,
            "prepare_launch",
            _agent_params(
                name=name,
                configs_dir=configs_dir,
                build_id=build_id,
                model_ref=model_ref,
                revision=revision,
            ),
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)


def _agent_params(**values) -> dict[str, str]:
    return {key: str(value) for key, value in values.items() if value is not None}


def _launch_override_params(
    *,
    build_id: str | None,
    model_ref: str | None,
    revision: str | None,
) -> dict[str, str | None]:
    return {
        "build_id": build_id,
        "model_ref": model_ref,
        "revision": revision,
    }


def _format_bytes(value: object) -> str:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        size = 0
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.1f} KB"
    return f"{size} B"


def _launch_agent_params(**values) -> dict[str, str]:
    params = _agent_params(**values)
    params["run_id"] = uuid.uuid4().hex
    return params


def _load_targets_or_exit() -> TargetsRegistry:
    try:
        return load_targets_file()
    except ValueError as exc:
        typer.echo(f"ERROR: Unable to load targets: {exc}", err=True)
        raise typer.Exit(2) from exc


def _target_client_for_name_or_exit(target_name: str) -> TargetClient:
    target = _target_config_for_name_or_exit(target_name)
    return _target_client_for_config_or_exit(target)


def _target_config_for_name_or_exit(target_name: str) -> TargetConfig:
    registry = _load_targets_or_exit()
    try:
        return registry.by_name(target_name)
    except KeyError as exc:
        available = ", ".join(target.name for target in registry.targets) or "none"
        typer.echo(f"ERROR: Unknown target: {target_name}", err=True)
        typer.echo(f"Available targets: {available}", err=True)
        raise typer.Exit(2) from exc


def _target_client_for_config_or_exit(target: TargetConfig) -> TargetClient:
    try:
        return target_client_for_config(target)
    except ValueError as exc:
        typer.echo(f"ERROR: Unable to create target client: {exc}", err=True)
        raise typer.Exit(2) from exc


def _discover_agent_command_for_target_or_exit(
    target: TargetConfig,
    *,
    persist: bool = False,
) -> TargetConfig:
    if target.transport is not TransportKind.SSH or target.agent_command is not None:
        return target
    try:
        discovery = discover_ssh_agent_command(target)
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, target_name=target.name)
    discovered = target.model_copy(update={"agent_command": discovery.agent_command})
    if persist:
        upsert_target_file(discovered)
    return discovered


def _target_agent_report_or_exit(target_name: str) -> dict[str, Any]:
    target = _target_config_for_name_or_exit(target_name)
    target = _discover_agent_command_for_target_or_exit(target, persist=True)
    try:
        return _target_call(_target_client_for_config_or_exit(target), "diagnose")
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, target_name=target.name)


def _bootstrap_discover_or_install_agent(
    target: TargetConfig,
    *,
    install: bool,
    install_spec: str,
) -> tuple[TargetConfig, str]:
    if target.agent_command is not None:
        return target, "provided"
    try:
        discovery = discover_ssh_agent_command(target)
    except TargetCallError as exc:
        if exc.code != "command-not-found" or not install:
            _echo_target_error_or_exit(exc, target_name=target.name)
        try:
            install_ssh_agent(target, install_spec=install_spec)
            discovery = discover_ssh_agent_command(target)
        except TargetCallError as install_exc:
            _echo_target_error_or_exit(install_exc, target_name=target.name)
        return (
            target.model_copy(update={"agent_command": discovery.agent_command}),
            f"installed {discovery.agent_command[0]}",
        )
    return (
        target.model_copy(update={"agent_command": discovery.agent_command}),
        f"discovered {discovery.agent_command[0]}",
    )


def _agent_call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    target_name: str = "local",
) -> dict[str, Any]:
    return _target_call(_target_client_for_name_or_exit(target_name), method, params)


async def _agent_call_async(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    target_name: str = "local",
) -> dict[str, Any]:
    return await _target_call_async(
        _target_client_for_name_or_exit(target_name),
        method,
        params,
    )


def _target_call(
    client: TargetClient, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        return asyncio.run(_target_call_async(client, method, params))
    except AgentTokenError as exc:
        raise TargetCallError(
            "agent-auth-required",
            "controller agent token is malformed",
            {"reason": "capability-token-misconfigured"},
        ) from exc


async def _target_call_async(
    client: TargetClient, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        await client.connect()
        try:
            return await client.call(method, params)
        finally:
            await client.disconnect()
    except AgentTokenError as exc:
        raise TargetCallError(
            "agent-auth-required",
            "controller agent token is malformed",
            {"reason": "capability-token-misconfigured"},
        ) from exc


def _echo_target_error_or_exit(
    exc: TargetCallError,
    *,
    fallback_name: str | None = None,
    target_name: str | None = None,
) -> None:
    remediation = remediation_for_error(
        exc.code,
        target_name=target_name,
        details=exc.details,
    )
    if remediation is not None:
        typer.echo(f"ERROR {remediation.label}: {exc.message}", err=True)
        for line in remediation.extra_lines:
            typer.echo(line, err=True)
        typer.echo(remediation.fix, err=True)
        raise typer.Exit(2) from exc
    if exc.code == "unknown-config":
        name = str(exc.details.get("name") or fallback_name or "unknown")
        available = ", ".join(str(item) for item in exc.details.get("available", [])) or "none"
        typer.echo(f"ERROR: Unknown config: {name}", err=True)
        typer.echo(f"Available configs: {available}", err=True)
        raise typer.Exit(2) from exc
    if exc.code == "invalid-config":
        name = str(exc.details.get("name") or fallback_name or "unknown")
        typer.echo(f"ERROR: Invalid config: {name}", err=True)
        validation = exc.details.get("validation")
        if isinstance(validation, dict):
            for item in validation.get("errors", []):
                if isinstance(item, dict):
                    field = str(item.get("field") or "config")
                    message = str(item.get("message") or "")
                    typer.echo(f"{field}: {message}", err=True)
                else:
                    typer.echo(str(item), err=True)
        for item in exc.details.get("matches", []):
            typer.echo(f"{Path(item['path']).name}: {'; '.join(item['errors'])}", err=True)
        raise typer.Exit(2) from exc
    if exc.code == "preflight-failed":
        kind = str(exc.details.get("kind") or "PREFLIGHT_FAILED")
        detail = str(exc.details.get("detail") or exc.message)
        typer.echo(f"ERROR {kind}: {detail}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"ERROR: {exc.message}", err=True)
    raise typer.Exit(2) from exc


def _echo_command_not_found_text(command: str) -> None:
    typer.echo(
        (
            f"ERROR: Command not found: {command}. "
            "install vLLM or set command.entrypoint: module."
        ),
        err=True,
    )


def _echo_agent_start_error_or_exit(exc: TargetCallError, fallback_command: str) -> None:
    if exc.code == "command-not-found":
        command = str(exc.details.get("command") or fallback_command)
        _echo_command_not_found_text(command)
        raise typer.Exit(2) from exc
    _echo_target_error_or_exit(exc)


def _fallback_command_from_prepared(prepared: dict[str, Any]) -> str:
    try:
        return str(prepared["build"]["argv"][0])
    except (KeyError, IndexError, TypeError):
        return "vllm"


async def _run_attached_cli(
    client: TargetClient,
    name: str,
    configs_dir: Path | None,
    prepared: dict[str, Any],
    *,
    build_id: str | None = None,
    model_ref: str | None = None,
    revision: str | None = None,
) -> int:
    await client.connect()
    interrupt_event = _install_sigint_event()
    try:
        try:
            launch = await client.call(
                "launch",
                _launch_agent_params(
                    name=name,
                    configs_dir=configs_dir,
                    build_id=build_id,
                    model_ref=model_ref,
                    revision=revision,
                ),
            )
        except TargetCallError as exc:
            _echo_agent_start_error_or_exit(exc, _fallback_command_from_prepared(prepared))
            return 2
        run_id = str(launch["run_id"])
        wait_task = asyncio.create_task(client.call("wait", {"run_id": run_id}))
        events = client.subscribe([run_id], resume_from="live")
        interrupt_task = (
            asyncio.create_task(interrupt_event.wait())
            if interrupt_event is not None
            else None
        )
        stream_task = asyncio.create_task(
            _echo_attached_event_stream_until_exit(events, wait_task)
        )
        try:
            wait_for: set[asyncio.Task] = {stream_task}
            if interrupt_task is not None:
                wait_for.add(interrupt_task)
            done, _pending = await asyncio.wait(
                wait_for,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupt_task is not None and interrupt_task in done:
                return await _stop_attached_cli_run(client, run_id, wait_task)
            return await stream_task
        except (KeyboardInterrupt, asyncio.CancelledError):
            return await _stop_attached_cli_run(client, run_id, wait_task)
        finally:
            stream_task.cancel()
            if interrupt_task is not None:
                interrupt_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(
                    *(task for task in (stream_task, interrupt_task) if task is not None),
                    return_exceptions=True,
                )
            await events.aclose()
    finally:
        if interrupt_event is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
        await client.disconnect()


def _install_sigint_event() -> asyncio.Event | None:
    event = asyncio.Event()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, event.set)
    except (NotImplementedError, RuntimeError):
        return None
    return event


async def _stop_attached_cli_run(
    client: TargetClient,
    run_id: str,
    wait_task: asyncio.Task[dict[str, Any]],
) -> int:
    await asyncio.shield(client.call("stop", {"run_id": run_id}))
    result = await asyncio.shield(wait_task)
    return int(result.get("returncode") or 0)


async def _run_detached_cli(
    client: TargetClient,
    name: str,
    configs_dir: Path | None,
    prepared: dict[str, Any],
    *,
    build_id: str | None = None,
    model_ref: str | None = None,
    revision: str | None = None,
) -> None:
    await client.connect()
    try:
        try:
            launch = await client.call(
                "launch",
                _launch_agent_params(
                    name=name,
                    configs_dir=configs_dir,
                    build_id=build_id,
                    model_ref=model_ref,
                    revision=revision,
                ),
            )
        except TargetCallError as exc:
            _echo_agent_start_error_or_exit(exc, _fallback_command_from_prepared(prepared))
            return
        typer.echo(f"detached run started: {launch['run_id']}")
    finally:
        await client.disconnect()


async def _echo_attached_event_stream_until_exit(events, wait_task) -> int:
    async for event in events:
        if event.get("event") == "log" and event.get("kind") == "committed":
            typer.echo(str(event.get("text", "")))
        if event.get("event") == "exited":
            break
    result = await wait_task
    return int(result.get("returncode") or 0)


async def _run_agent_job_cli(
    client: TargetClient,
    method: str,
    params: dict[str, Any],
    *,
    json_output: bool = False,
) -> int:
    job_id = str(params["job_id"])
    await client.connect()
    events = client.subscribe([job_id], resume_from="live")
    try:
        try:
            await client.call(method, params)
        except TargetCallError as exc:
            _echo_target_error_or_exit(exc)
            return 2
        return await _echo_job_event_stream_until_done(
            events, job_id, json_output=json_output
        )
    finally:
        await events.aclose()
        await client.disconnect()


async def _echo_job_event_stream_until_done(
    events, job_id: str, *, json_output: bool = False
) -> int:
    async for event in events:
        if event.get("job_id") != job_id:
            continue
        if event.get("event") == "job_progress":
            if json_output:
                continue
            text = event.get("text")
            if isinstance(text, str) and text:
                typer.echo(text)
            continue
        if event.get("event") != "job_done":
            continue
        if json_output:
            _echo_json(dict(event))
            return 0 if event.get("ok") else 2
        detail = str(event.get("detail") or "")
        if event.get("ok"):
            typer.echo(f"DONE\t{job_id}\t{detail}")
            return 0
        typer.echo(f"ERROR\t{job_id}\t{detail}", err=True)
        return 2
    typer.echo(f"ERROR\t{job_id}\tjob stream ended before completion", err=True)
    return 2


async def _create_build_cli(
    client: TargetClient,
    params: dict[str, Any],
    *,
    target_name: str = "local",
) -> int:
    try:
        await _target_call_async(
            client,
            "check_build_prerequisites",
            _build_prerequisite_params(params),
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, target_name=target_name)
        return 2
    return await _run_agent_job_cli(client, "create_build", params)


def _build_prerequisite_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key != "job_id"}


async def _smoke_config_cli(
    client: TargetClient,
    prepared: dict[str, Any],
    name: str,
    configs_dir: Path | None,
    *,
    build_id: str | None = None,
    model_ref: str | None = None,
    revision: str | None = None,
) -> int:
    cfg = ModelConfig.model_validate(prepared["config"])
    if cfg.launch.mode.value == "detached":
        return await _smoke_detached_cli(
            client,
            prepared,
            name,
            configs_dir,
            build_id=build_id,
            model_ref=model_ref,
            revision=revision,
        )
    return await _smoke_attached_cli(
        client,
        prepared,
        name,
        configs_dir,
        build_id=build_id,
        model_ref=model_ref,
        revision=revision,
    )


async def _smoke_tui_config_cli(
    name: str,
    configs_dir: Path | None,
    target_name: str = "local",
    *,
    build_id: str | None = None,
    model_ref: str | None = None,
    revision: str | None = None,
) -> int:
    tui = VelaApp(
        configs_dir=configs_dir,
        target_name=target_name,
        launch_overrides=_launch_override_params(
            build_id=build_id,
            model_ref=model_ref,
            revision=revision,
        ),
    )
    try:
        async with tui.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            try:
                tui.select_config(name)
            except KeyError:
                typer.echo(f"ERROR: Unknown config: {name}", err=True)
                return 2
            tui.action_load()
            cfg = tui.current_config
            timeout = (cfg.launch.ready_timeout_seconds if cfg is not None else 60) + 10
            if not await _wait_for_tui_phase(tui, Phase.READY, timeout=timeout):
                detail = tui.error_text or tui.health_detail or tui.status_text
                typer.echo(f"ERROR: TUI smoke did not reach READY: {detail}", err=True)
                tui.action_stop()
                await _wait_for_tui_stopped(
                    tui, timeout=_smoke_tui_stop_timeout(cfg)
                )
                return 2
            if not tui.ready_url:
                typer.echo("ERROR: TUI smoke reached READY without reachable_url", err=True)
                return 2
            url = tui.ready_url
            models = ",".join(tui.served_models)
            suffix = f" models={models}" if models else ""
            run_id = tui.current_run_id or tui.reattached_run_id
            run_suffix = f" run_id={run_id}" if run_id else ""
            typer.echo(f"READY {url}{suffix}{run_suffix}")
            tui.action_stop()
            if not await _wait_for_tui_stopped(
                tui, timeout=_smoke_tui_stop_timeout(cfg)
            ):
                typer.echo("ERROR: TUI smoke server did not stop cleanly", err=True)
                return 2
            return 0
    finally:
        if tui.current_run_id is not None or tui.reattached_run_id is not None:
            tui.action_stop()
            await _wait_for_tui_stopped(
                tui, timeout=_smoke_tui_stop_timeout(tui.current_config)
            )


def _smoke_tui_stop_timeout(cfg: ModelConfig | None) -> float:
    if cfg is None:
        return 10
    if cfg.command.runtime is RuntimeKind.DOCKER and cfg.command.docker is not None:
        return max(10, float(cfg.command.docker.stop_grace_seconds) + 10)
    return 10


async def _wait_for_tui_phase(tui: VelaApp, phase: Phase, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if tui.phase is phase:
            return True
        if tui.phase in {Phase.ERROR, Phase.STOPPED}:
            return False
        await asyncio.sleep(0.05)
    return False


async def _wait_for_tui_stopped(tui: VelaApp, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if tui.current_run_id is None and tui.reattached_run_id is None:
            return True
        await asyncio.sleep(0.05)
    return False


async def _smoke_attached_cli(
    client: TargetClient,
    prepared: dict[str, Any],
    name: str,
    configs_dir: Path | None,
    *,
    build_id: str | None = None,
    model_ref: str | None = None,
    revision: str | None = None,
) -> int:
    cfg = ModelConfig.model_validate(prepared["config"])
    await client.connect()
    try:
        try:
            launch = await client.call(
                "launch",
                _launch_agent_params(
                    name=name,
                    configs_dir=configs_dir,
                    build_id=build_id,
                    model_ref=model_ref,
                    revision=revision,
                ),
            )
        except TargetCallError as exc:
            _echo_agent_start_error_or_exit(exc, _fallback_command_from_prepared(prepared))
            return 2
        run_id = str(launch["run_id"])
        read_task = asyncio.create_task(client.call("wait", {"run_id": run_id}))
        health_code = await _wait_target_until_ready_or_exit(
            client,
            run_id,
            cfg,
            read_task=read_task,
        )
        if health_code == 0:
            try:
                await client.call(
                    "stop",
                    {
                        "run_id": run_id,
                        "interrupt_timeout": 2,
                        "terminate_timeout": 2,
                    },
                )
            except TargetCallError as exc:
                typer.echo(f"WARNING: unable to stop smoke run: {exc}", err=True)
        if not read_task.done():
            await read_task
        return health_code
    finally:
        await client.disconnect()


async def _smoke_detached_cli(
    client: TargetClient,
    prepared: dict[str, Any],
    name: str,
    configs_dir: Path | None,
    *,
    build_id: str | None = None,
    model_ref: str | None = None,
    revision: str | None = None,
) -> int:
    cfg = ModelConfig.model_validate(prepared["config"])
    await client.connect()
    try:
        try:
            launch = await client.call(
                "launch",
                _launch_agent_params(
                    name=name,
                    configs_dir=configs_dir,
                    build_id=build_id,
                    model_ref=model_ref,
                    revision=revision,
                ),
            )
        except TargetCallError as exc:
            _echo_agent_start_error_or_exit(exc, _fallback_command_from_prepared(prepared))
            return 2
        run_id = str(launch["run_id"])
        typer.echo(f"detached smoke run: {run_id}")
        health_code = await _wait_target_until_ready_or_exit(client, run_id, cfg)
        try:
            await client.call(
                "stop",
                {
                    "run_id": run_id,
                    "interrupt_timeout": 2,
                    "terminate_timeout": 2,
                },
            )
        except TargetCallError as exc:
            typer.echo(f"WARNING: unable to stop detached smoke run: {exc}", err=True)
        return health_code
    finally:
        await client.disconnect()



async def _wait_target_until_ready_or_exit(
    client: TargetClient,
    run_id: str,
    cfg: ModelConfig,
    *,
    read_task: asyncio.Task[dict[str, Any]] | None = None,
) -> int:
    probe_task = asyncio.create_task(client.call("probe_until_ready", {"run_id": run_id}))
    wait_on = {probe_task}
    if read_task is not None:
        wait_on.add(read_task)
    done, _pending = await asyncio.wait(wait_on, return_when=asyncio.FIRST_COMPLETED)
    if probe_task in done:
        probe = probe_task.result()
        if probe.get("ready"):
            models = ",".join(probe.get("models") or [])
            suffix = f" models={models}" if models else ""
            url = probe.get("reachable_url")
            if not isinstance(url, str) or not url.strip():
                typer.echo(
                    "ERROR: Agent health response missing reachable_url",
                    err=True,
                )
                return 2
            typer.echo(f"READY {url}{suffix}")
            return 0
        error_kind = probe.get("error_kind")
        if error_kind is not None:
            typer.echo(f"ERROR {error_kind}: {probe.get('detail', '')}", err=True)
            return 2
        return 1
    if read_task is not None and read_task in done:
        probe_task.cancel()
        result = read_task.result()
        return int(result.get("returncode") or 1)
    probe_task.cancel()
    return 1

@app.command("version")
def version() -> None:
    typer.echo(__version__)


@agent_app.command("connect")
def agent_connect(
    socket_path: Annotated[
        Path | None,
        typer.Option("--socket", help="Connect stdio to an existing agent socket."),
    ] = None,
) -> None:
    if socket_path is not None:
        from vela.agent.daemon import start_agent_daemon_process
        from vela.agent.socket import bridge_stdio_to_unix_socket

        if not socket_path.exists():
            status = start_agent_daemon_process(socket_path)
            if status["status"] != "running":
                typer.echo(_format_agent_status(status), err=True)
                raise typer.Exit(1)
        try:
            asyncio.run(bridge_stdio_to_unix_socket(socket_path))
        except OSError as exc:
            status = start_agent_daemon_process(socket_path)
            if status["status"] != "running":
                typer.echo(_format_agent_status(status), err=True)
                raise typer.Exit(1) from exc
            asyncio.run(bridge_stdio_to_unix_socket(socket_path))
        return

    from vela.agent.stdio import serve_stdio_agent

    asyncio.run(serve_stdio_agent(LocalAgent()))


@agent_app.command("gen-token")
def agent_gen_token(
    entropy_bytes: Annotated[
        int,
        typer.Option(
            "--bytes",
            help="Random bytes of token entropy; 16 bytes is the minimum.",
        ),
    ] = DEFAULT_AGENT_TOKEN_BYTES,
    install: Annotated[
        bool,
        typer.Option("--install", help="Write the token to the default agent token file."),
    ] = False,
    install_path: Annotated[
        Path | None,
        typer.Option("--install-path", help="Override the token install path."),
    ] = None,
    target_name: Annotated[
        str | None,
        typer.Option("--target", help="Also install the token on a configured target."),
    ] = None,
) -> None:
    if target_name is not None and not install:
        typer.echo("ERROR: --target requires --install", err=True)
        raise typer.Exit(2)
    if entropy_bytes < MIN_AGENT_TOKEN_BYTES:
        typer.echo(
            f"ERROR: agent token entropy must be at least {MIN_AGENT_TOKEN_BYTES} bytes",
            err=True,
        )
        raise typer.Exit(2)
    token = generate_agent_token(entropy_bytes)
    if install:
        try:
            token_path, _token = install_agent_token(token, path=install_path)
        except AgentTokenError as exc:
            typer.echo(f"ERROR: unable to install agent token: {exc}", err=True)
            raise typer.Exit(2) from exc
        typer.echo(f"installed agent token\t{token_path}")
        if target_name is not None:
            target = _target_config_for_name_or_exit(target_name)
            try:
                result = _target_call(
                    _target_client_for_config_or_exit(target),
                    "write_agent_token",
                    {"token": token},
                )
            except TargetCallError as exc:
                _echo_target_error_or_exit(exc, target_name=target.name)
            typer.echo(f"installed target agent token\t{target.name}\t{result['path']}")
        return
    typer.echo(token)


@agent_app.command("run")
def agent_run(
    socket_path: Annotated[
        Path | None,
        typer.Option("--socket", help="Unix socket path for the foreground agent daemon."),
    ] = None,
    idle_timeout: Annotated[
        float | None,
        typer.Option(
            "--idle-timeout",
            help="Exit after this many idle seconds with no connections or active runs.",
        ),
    ] = None,
) -> None:
    from vela.agent.daemon import run_agent_daemon

    asyncio.run(run_agent_daemon(socket_path=socket_path, idle_timeout_seconds=idle_timeout))


@agent_app.command("start")
def agent_start(
    socket_path: Annotated[
        Path | None,
        typer.Option("--socket", help="Unix socket path for the agent daemon."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable daemon start result."),
    ] = False,
) -> None:
    import json

    from vela.agent.daemon import start_agent_daemon_process

    result = start_agent_daemon_process(socket_path)
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True))
    else:
        typer.echo(_format_agent_status(result))
    if result["status"] != "running":
        raise typer.Exit(1)


@agent_app.command("status")
def agent_status(
    socket_path: Annotated[
        Path | None,
        typer.Option("--socket", help="Unix socket path for the agent daemon."),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Inspect the agent on a configured target."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable daemon status."),
    ] = False,
) -> None:
    import json

    from vela.agent.daemon import inspect_agent_daemon

    if target is not None:
        report = _target_agent_report_or_exit(target)
        if json_output:
            typer.echo(json.dumps({"target": target, "diagnose": report}, sort_keys=True))
            return
        typer.echo(f"target\t{target}")
        for line in _target_report_lines(report):
            typer.echo(line)
        return

    status = inspect_agent_daemon(socket_path)
    if json_output:
        typer.echo(json.dumps(status, sort_keys=True))
    else:
        typer.echo(_format_agent_status(status))
    if status["status"] != "running":
        raise typer.Exit(1)


@agent_app.command("stop")
def agent_stop(
    socket_path: Annotated[
        Path | None,
        typer.Option("--socket", help="Unix socket path for the agent daemon."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable daemon stop result."),
    ] = False,
) -> None:
    import json

    from vela.agent.daemon import stop_agent_daemon

    result = stop_agent_daemon(socket_path)
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True))
    else:
        typer.echo(_format_agent_status(result))
    if result["status"] != "stopped":
        raise typer.Exit(1)


@agent_app.command("restart")
def agent_restart(
    socket_path: Annotated[
        Path | None,
        typer.Option("--socket", help="Unix socket path for the agent daemon."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable daemon restart result."),
    ] = False,
) -> None:
    import json

    from vela.agent.daemon import restart_agent_daemon_process

    result = restart_agent_daemon_process(socket_path)
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True))
    else:
        typer.echo(_format_agent_status(result))
    if result["status"] != "running":
        raise typer.Exit(1)


def _format_agent_status(status: dict[str, Any]) -> str:
    if status["status"] == "stopped":
        return f"stopped pid={status.get('pid')} socket={status.get('socket_path')}"
    if status["status"] == "running":
        return f"running pid={status.get('pid')} socket={status.get('socket_path')}"
    return f"{status['status']} socket={status.get('socket_path')}"


def main() -> None:
    app()


if __name__ == "__main__":
    main()
