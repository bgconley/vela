from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer

from vllm_loader import __version__
from vllm_loader.agent.local import LocalAgent, TargetCallError
from vllm_loader.config.schema import ModelConfig
from vllm_loader.config.targets import (
    TargetConfig,
    TargetsRegistry,
    TransportKind,
    load_targets_file,
    remove_target_file,
    upsert_target_file,
)
from vllm_loader.engine.phases import Phase
from vllm_loader.monitoring.health import probe_host_for
from vllm_loader.transport.client import TargetClient
from vllm_loader.transport.factory import target_client_for_config
from vllm_loader.tui.app import VllmLoaderApp

app = typer.Typer(
    no_args_is_help=False, invoke_without_command=True, help="Launch and monitor vLLM servers."
)
agent_app = typer.Typer(help="Run or connect to the local vLLM Loader agent.")
targets_app = typer.Typer(help="Manage controller target registry.")
app.add_typer(agent_app, name="agent")
app.add_typer(targets_app, name="targets")


@app.callback(invoke_without_command=True)
def interactive(
    ctx: typer.Context,
    version_requested: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show vLLM Loader version and exit.",
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
        VllmLoaderApp(
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
    return state_home / "vllm-loader" / "debug.jsonl"


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
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Remote working directory."),
    ] = None,
    venv: Annotated[
        Path | None,
        typer.Option("--venv", help="Remote virtualenv path."),
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
            workdir=workdir,
            venv=venv,
            ssh_opts_env=ssh_opts_env,
        )
        upsert_target_file(target)
    except ValueError as exc:
        typer.echo(f"ERROR: Unable to add target: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"added target {name}")


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
    try:
        handshake = _target_call(_target_client_for_name_or_exit(name), "handshake")
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc)
    typer.echo(
        f"{name}\tok\t"
        f"agent={handshake.get('agent_version', 'unknown')}\t"
        f"protocol={handshake.get('protocol_version', 'unknown')}"
    )


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
) -> None:
    try:
        result = _agent_call(
            "preview",
            _agent_params(name=name, configs_dir=configs_dir),
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
) -> None:
    client = _target_client_for_name_or_exit(target)
    if preview_only:
        try:
            result = _target_call(
                client,
                "preview",
                _agent_params(name=name, configs_dir=configs_dir),
            )
        except TargetCallError as exc:
            _echo_target_error_or_exit(exc, fallback_name=name)
        typer.echo(result["preview"])
        _echo_warnings(result.get("warnings", []))
        return
    prepared = _prepare_launch_with_client_or_exit(client, name, configs_dir)
    cfg = ModelConfig.model_validate(prepared["config"])
    if cfg.launch.mode.value == "detached":
        asyncio.run(_run_detached_cli(client, name, configs_dir, prepared))
        return

    raise typer.Exit(asyncio.run(_run_attached_cli(client, name, configs_dir, prepared)))


@app.command("smoke")
def smoke_config(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
) -> None:
    client = _target_client_for_name_or_exit(target)
    prepared = _prepare_launch_with_client_or_exit(client, name, configs_dir)
    raise typer.Exit(asyncio.run(_smoke_config_cli(client, prepared, name, configs_dir)))


@app.command("smoke-tui")
def smoke_tui_config(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
    target: Annotated[str, typer.Option("--target", help="Execution target name.")] = "local",
) -> None:
    client = _target_client_for_name_or_exit(target)
    prepared = _prepare_launch_with_client_or_exit(client, name, configs_dir)
    cfg = ModelConfig.model_validate(prepared["config"])
    raise typer.Exit(asyncio.run(_smoke_tui_config_cli(cfg.name, configs_dir)))


def _echo_warnings(warnings) -> None:
    for warning in warnings:
        typer.echo(f"WARNING: {warning}", err=True)


def _prepare_launch_with_client_or_exit(
    client: TargetClient, name: str, configs_dir: Path | None
) -> dict[str, Any]:
    try:
        return _target_call(
            client,
            "prepare_launch",
            _agent_params(name=name, configs_dir=configs_dir),
        )
    except TargetCallError as exc:
        _echo_target_error_or_exit(exc, fallback_name=name)


def _agent_params(**values) -> dict[str, str]:
    return {key: str(value) for key, value in values.items() if value is not None}


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
    registry = _load_targets_or_exit()
    try:
        target = registry.by_name(target_name)
    except KeyError as exc:
        available = ", ".join(target.name for target in registry.targets) or "none"
        typer.echo(f"ERROR: Unknown target: {target_name}", err=True)
        typer.echo(f"Available targets: {available}", err=True)
        raise typer.Exit(2) from exc
    return target_client_for_config(target)


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
    return asyncio.run(_target_call_async(client, method, params))


async def _target_call_async(
    client: TargetClient, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    await client.connect()
    try:
        return await client.call(method, params)
    finally:
        await client.disconnect()


def _echo_target_error_or_exit(exc: TargetCallError, *, fallback_name: str | None = None) -> None:
    if exc.code == "unknown-config":
        name = str(exc.details.get("name") or fallback_name or "unknown")
        available = ", ".join(str(item) for item in exc.details.get("available", [])) or "none"
        typer.echo(f"ERROR: Unknown config: {name}", err=True)
        typer.echo(f"Available configs: {available}", err=True)
        raise typer.Exit(2) from exc
    if exc.code == "invalid-config":
        name = str(exc.details.get("name") or fallback_name or "unknown")
        typer.echo(f"ERROR: Invalid config: {name}", err=True)
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
) -> int:
    await client.connect()
    try:
        try:
            launch = await client.call(
                "launch",
                _launch_agent_params(name=name, configs_dir=configs_dir),
            )
        except TargetCallError as exc:
            _echo_agent_start_error_or_exit(exc, _fallback_command_from_prepared(prepared))
            return 2
        run_id = str(launch["run_id"])
        wait_task = asyncio.create_task(client.call("wait", {"run_id": run_id}))
        events = client.subscribe([run_id], resume_from="live")
        try:
            return await _echo_attached_event_stream_until_exit(events, wait_task)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await asyncio.shield(client.call("stop", {"run_id": run_id}))
            result = await asyncio.shield(wait_task)
            return int(result.get("returncode") or 0)
        finally:
            await events.aclose()
    finally:
        await client.disconnect()


async def _run_detached_cli(
    client: TargetClient,
    name: str,
    configs_dir: Path | None,
    prepared: dict[str, Any],
) -> None:
    await client.connect()
    try:
        try:
            launch = await client.call(
                "launch",
                _launch_agent_params(name=name, configs_dir=configs_dir),
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


async def _smoke_config_cli(
    client: TargetClient,
    prepared: dict[str, Any],
    name: str,
    configs_dir: Path | None,
) -> int:
    cfg = ModelConfig.model_validate(prepared["config"])
    if cfg.launch.mode.value == "detached":
        return await _smoke_detached_cli(client, prepared, name, configs_dir)
    return await _smoke_attached_cli(client, prepared, name, configs_dir)


async def _smoke_tui_config_cli(name: str, configs_dir: Path | None) -> int:
    tui = VllmLoaderApp(configs_dir=configs_dir)
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
                await _wait_for_tui_stopped(tui, timeout=10)
                return 2
            url = tui.ready_url or (_server_url(tui.current_config) if tui.current_config else "")
            models = ",".join(tui.served_models)
            suffix = f" models={models}" if models else ""
            typer.echo(f"READY {url}{suffix}")
            tui.action_stop()
            if not await _wait_for_tui_stopped(tui, timeout=10):
                typer.echo("ERROR: TUI smoke server did not stop cleanly", err=True)
                return 2
            return 0
    finally:
        if tui.current_run_id is not None or tui.reattached_run_id is not None:
            tui.action_stop()
            await _wait_for_tui_stopped(tui, timeout=10)


async def _wait_for_tui_phase(tui: VllmLoaderApp, phase: Phase, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if tui.phase is phase:
            return True
        if tui.phase in {Phase.ERROR, Phase.STOPPED}:
            return False
        await asyncio.sleep(0.05)
    return False


async def _wait_for_tui_stopped(tui: VllmLoaderApp, *, timeout: float) -> bool:
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
) -> int:
    cfg = ModelConfig.model_validate(prepared["config"])
    await client.connect()
    try:
        try:
            launch = await client.call(
                "launch",
                _launch_agent_params(name=name, configs_dir=configs_dir),
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
) -> int:
    cfg = ModelConfig.model_validate(prepared["config"])
    await client.connect()
    try:
        try:
            launch = await client.call(
                "launch",
                _launch_agent_params(name=name, configs_dir=configs_dir),
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
            typer.echo(f"READY {_server_url(cfg)}{suffix}")
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


def _server_url(cfg) -> str:
    return f"http://{probe_host_for(cfg.server)}:{cfg.server.port}"


@app.command("version")
def version() -> None:
    typer.echo(__version__)


@agent_app.command("connect")
def agent_connect() -> None:
    from vllm_loader.agent.stdio import serve_stdio_agent

    asyncio.run(serve_stdio_agent(LocalAgent()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
