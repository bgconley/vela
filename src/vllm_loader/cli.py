from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from vllm_loader import __version__
from vllm_loader.config.loader import load_registry
from vllm_loader.engine.command_builder import build_command
from vllm_loader.engine.phases import Phase
from vllm_loader.engine.preflight import check_launch_preflight
from vllm_loader.engine.profile import (
    VllmProfileError,
    detect_vllm_version_for_config,
    select_profile_for_config,
)
from vllm_loader.engine.sidecar import stop_sidecar_from_system, verify_sidecar_from_system
from vllm_loader.monitoring.health import HealthEvent, probe_host_for, probe_loop
from vllm_loader.tui.app import VllmLoaderApp

app = typer.Typer(
    no_args_is_help=False, invoke_without_command=True, help="Launch and monitor vLLM servers."
)


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


@app.command("list")
def list_configs(
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
) -> None:
    registry = load_registry(configs_dir)
    for item in registry.valid:
        typer.echo(f"{item.config.name}\t{item.config.model}")
    for item in registry.invalid:
        typer.echo(f"INVALID {item.path.name}\t{'; '.join(item.errors)}")


@app.command("preview")
def preview(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
) -> None:
    registry = load_registry(configs_dir)
    cfg = _config_by_name_or_exit(registry, name)
    result = _build_command_or_exit(cfg)
    typer.echo(result.preview)
    _echo_command_warnings(result)


@app.command("run")
def run_config(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
    preview_only: Annotated[
        bool, typer.Option("--preview", help="Print command instead of launching.")
    ] = False,
) -> None:
    registry = load_registry(configs_dir)
    cfg = _config_by_name_or_exit(registry, name)
    result = _build_command_or_exit(cfg)
    if preview_only:
        typer.echo(result.preview)
        _echo_command_warnings(result)
        return
    _launch_preflight_or_exit(cfg, result.cwd)
    if cfg.launch.mode.value == "detached":
        from vllm_loader.engine.process_manager import start_detached

        try:
            launch = start_detached(
                cfg,
                result,
                secrets=[cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")],
                vllm_version=detect_vllm_version_for_config(cfg),
                vllm_version_profile=result.metadata.get("vllm_version_profile"),
            )
        except FileNotFoundError as exc:
            _echo_command_not_found(exc, result.argv[0])
            raise typer.Exit(2) from exc
        typer.echo(f"detached run started: {launch.run_id}")
        typer.echo(f"sidecar: {launch.sidecar_path}")
        typer.echo(f"log: {launch.log_path}")
        return

    raise typer.Exit(asyncio.run(_run_attached_cli(cfg, result)))


@app.command("smoke")
def smoke_config(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
) -> None:
    registry = load_registry(configs_dir)
    cfg = _config_by_name_or_exit(registry, name)
    result = _build_command_or_exit(cfg)
    _launch_preflight_or_exit(cfg, result.cwd)
    raise typer.Exit(asyncio.run(_smoke_config_cli(cfg, result)))


@app.command("smoke-tui")
def smoke_tui_config(
    name: str,
    configs_dir: Annotated[Path | None, typer.Option("--configs-dir")] = None,
) -> None:
    registry = load_registry(configs_dir)
    cfg = _config_by_name_or_exit(registry, name)
    result = _build_command_or_exit(cfg)
    _launch_preflight_or_exit(cfg, result.cwd)
    raise typer.Exit(asyncio.run(_smoke_tui_config_cli(cfg.name, configs_dir)))


def _build_command_or_exit(cfg):
    try:
        return build_command(cfg, select_profile_for_config(cfg))
    except VllmProfileError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(2) from exc


def _echo_command_warnings(result) -> None:
    for warning in result.warnings:
        typer.echo(f"WARNING: {warning}", err=True)


def _launch_preflight_or_exit(cfg, cwd: Path) -> None:
    failure = check_launch_preflight(cfg, cwd=cwd)
    if failure is None:
        return
    typer.echo(f"ERROR {failure.kind.value}: {failure.detail}", err=True)
    raise typer.Exit(2)


def _config_by_name_or_exit(registry, name: str):
    try:
        return registry.by_name(name)
    except KeyError as exc:
        invalid_matches = [
            item
            for item in registry.invalid
            if item.raw_name == name or (item.raw_name is None and item.path.stem == name)
        ]
        if invalid_matches:
            typer.echo(f"ERROR: Invalid config: {name}", err=True)
            for item in invalid_matches:
                typer.echo(f"{item.path.name}: {'; '.join(item.errors)}", err=True)
            raise typer.Exit(2) from exc
        available = ", ".join(item.config.name for item in registry.valid) or "none"
        typer.echo(f"ERROR: Unknown config: {name}", err=True)
        typer.echo(f"Available configs: {available}", err=True)
        raise typer.Exit(2) from exc


def _echo_command_not_found(exc: FileNotFoundError, fallback_command: str) -> None:
    command = str(exc.filename or fallback_command)
    typer.echo(
        (
            f"ERROR: Command not found: {command}. "
            "install vLLM or set command.entrypoint: module."
        ),
        err=True,
    )


async def _run_attached_cli(cfg, result) -> int:
    from vllm_loader.engine.process_manager import start_attached

    run_dir = cfg.run_artifacts_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = start_attached(
            result,
            log_path=run_dir / f"{cfg.name}.run.log",
            secrets=[cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")],
            emit=lambda record: typer.echo(record.text) if record.kind == "committed" else None,
        )
    except FileNotFoundError as exc:
        _echo_command_not_found(exc, result.argv[0])
        return 2
    try:
        return int(await proc.read_loop() or 0)
    except KeyboardInterrupt:
        proc.stop()
        return int(await proc.read_loop() or 0)


async def _smoke_config_cli(cfg, result) -> int:
    if cfg.launch.mode.value == "detached":
        return await _smoke_detached_cli(cfg, result)
    return await _smoke_attached_cli(cfg, result)


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
        if tui.current_process and tui.current_process.proc.poll() is None:
            tui._mark_current_process_shutdown_intent()
            tui.current_process.stop(interrupt_timeout=2, terminate_timeout=2)
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
        if tui.current_process is None or tui.current_process.proc.poll() is not None:
            return True
        await asyncio.sleep(0.05)
    return False


async def _smoke_attached_cli(cfg, result) -> int:
    from vllm_loader.engine.process_manager import start_attached

    run_dir = cfg.run_artifacts_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = start_attached(
            result,
            log_path=run_dir / f"{cfg.name}.smoke.log",
            secrets=[cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")],
            emit=lambda record: typer.echo(record.text) if record.kind == "committed" else None,
        )
    except FileNotFoundError as exc:
        _echo_command_not_found(exc, result.argv[0])
        return 2
    read_task = asyncio.create_task(proc.read_loop())
    health_code = await _wait_until_ready_or_exit(cfg, read_task)
    if proc.proc.poll() is None:
        proc.stop(interrupt_timeout=2, terminate_timeout=2)
    if not read_task.done():
        await read_task
    return health_code


async def _smoke_detached_cli(cfg, result) -> int:
    from vllm_loader.engine.process_manager import start_detached

    try:
        launch = start_detached(
            cfg,
            result,
            secrets=[cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")],
            vllm_version=detect_vllm_version_for_config(cfg),
            vllm_version_profile=result.metadata.get("vllm_version_profile"),
        )
    except FileNotFoundError as exc:
        _echo_command_not_found(exc, result.argv[0])
        return 2
    typer.echo(f"detached smoke sidecar: {launch.sidecar_path}")
    health_code = await _wait_until_ready_or_exit(
        cfg,
        None,
        is_alive=lambda: _sidecar_is_alive(launch.sidecar_path),
    )
    try:
        stop_sidecar_from_system(launch.sidecar_path, interrupt_timeout=2, terminate_timeout=2)
    except Exception as exc:
        typer.echo(f"WARNING: unable to stop detached smoke run: {exc}", err=True)
    return health_code


async def _wait_until_ready_or_exit(
    cfg,
    read_task: asyncio.Task[int | None] | None,
    *,
    is_alive=None,
) -> int:
    status = {"code": 1}
    ready = asyncio.Event()

    def emit(event: HealthEvent) -> None:
        if event.ready:
            models = ",".join(event.models or [])
            suffix = f" models={models}" if models else ""
            typer.echo(f"READY {_server_url(cfg)}{suffix}")
            status["code"] = 0
            ready.set()
            return
        if event.error_kind is not None:
            typer.echo(f"ERROR {event.error_kind.value}: {event.detail}", err=True)
            status["code"] = 2
            ready.set()

    health_task = asyncio.create_task(
        probe_loop(
            cfg,
            emit=emit,
            is_process_alive=is_alive or (lambda: bool(read_task and not read_task.done())),
        )
    )
    ready_task = asyncio.create_task(ready.wait())
    wait_on = {health_task, ready_task}
    if read_task is not None:
        wait_on.add(read_task)
    done, _pending = await asyncio.wait(wait_on, return_when=asyncio.FIRST_COMPLETED)
    if read_task is not None and read_task in done and not ready_task.done():
        health_task.cancel()
        ready_task.cancel()
        return int(read_task.result() or 1)
    if health_task in done and not ready_task.done():
        ready_task.cancel()
        return status["code"]
    health_task.cancel()
    return status["code"]


def _server_url(cfg) -> str:
    return f"http://{probe_host_for(cfg.server)}:{cfg.server.port}"


def _sidecar_is_alive(sidecar_path: Path) -> bool:
    try:
        return bool(verify_sidecar_from_system(sidecar_path))
    except Exception:
        return False


@app.command("version")
def version() -> None:
    typer.echo(__version__)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
