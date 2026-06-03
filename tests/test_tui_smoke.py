from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_yaml
from rich.text import Text
from textual.screen import ModalScreen
from textual.widgets import ProgressBar, RichLog, Static
from textual.worker import WorkerState

from vllm_loader.agent import local as local_agent_module
from vllm_loader.agent.local import TargetCallError
from vllm_loader.config.loader import load_registry
from vllm_loader.engine.command_builder import build_command
from vllm_loader.engine.log_sink import LogRecord
from vllm_loader.engine.phases import ErrorKind, Phase
from vllm_loader.engine.process_manager import start_detached
from vllm_loader.engine.sidecar import Manifest, Sidecar, TrackedProcessMismatch
from vllm_loader.messages import (
    EngineError,
    GpuStatsUnavailable,
    GpuStatsUpdated,
    HealthChanged,
    LogLineCommitted,
    PhaseChanged,
    ProcessExited,
    ProgressUpdated,
    ServerReady,
)
from vllm_loader.monitoring.gpu import GpuPollResult, GpuSample
from vllm_loader.monitoring.health import HealthEvent
from vllm_loader.tui import app as tui_app_module
from vllm_loader.tui.app import VllmLoaderApp
from vllm_loader.tui.screens.confirm import ConfirmScreen


class RecordingConfigAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def handle(self, method: str, params: dict[str, str] | None = None):
        self.calls.append((method, params))
        if method == "list_configs":
            return {
                "valid": [
                    {
                        "path": "/agent/configs/alpha.yaml",
                        "name": "alpha",
                        "model": "org/alpha",
                        "target": None,
                        "warnings": [],
                        "config": {"name": "alpha", "model": "org/alpha"},
                    },
                    {
                        "path": "/agent/configs/beta.yaml",
                        "name": "beta",
                        "model": "org/beta",
                        "target": "blackbird",
                        "warnings": [],
                        "config": {
                            "name": "beta",
                            "target": "blackbird",
                            "model": "org/beta",
                        },
                    },
                ],
                "invalid": [],
            }
        if method == "preview":
            name = str((params or {}).get("name"))
            return {"preview": f"cwd=/agent\nvllm serve org/{name}", "warnings": []}
        raise AssertionError(f"unexpected method: {method}")

    def discover_detached_runs(self, runs_dirs):
        return []


class RecordingLaunchPrepareAgent(RecordingConfigAgent):
    def handle(self, method: str, params: dict[str, str] | None = None):
        if method == "prepare_launch":
            self.calls.append((method, params))
            raise TargetCallError(
                "preflight-failed",
                "agent-side missing model",
                {"kind": "MODEL_NOT_FOUND", "detail": "agent-side missing model"},
            )
        return super().handle(method, params)


class StopRecordingAgent(RecordingConfigAgent):
    def __init__(self) -> None:
        super().__init__()
        self.stop_calls: list[tuple[str, float, float]] = []

    def is_run_alive(self, run_id: str) -> bool:
        return run_id == "run-1"

    def stop_run(
        self, run_id: str, *, interrupt_timeout: float, terminate_timeout: float
    ) -> None:
        self.stop_calls.append((run_id, interrupt_timeout, terminate_timeout))


class HealthProbeRecordingAgent(StopRecordingAgent):
    def __init__(self) -> None:
        super().__init__()
        self.probe_calls: list[str] = []

    async def probe_run_until_ready(self, run_id: str, *, emit) -> None:
        self.probe_calls.append(run_id)
        emit(HealthEvent(ready=True, detail="ready from agent", models=["served"]))


class GpuRecordingAgent(RecordingConfigAgent):
    def __init__(self) -> None:
        super().__init__()
        self.sample_calls = 0

    def sample_gpus(self) -> GpuPollResult:
        self.sample_calls += 1
        return GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="A100",
                    memory_used_mb=1024,
                    memory_total_mb=81920,
                    utilization_percent=25,
                    temperature_c=42,
                    power_w=110,
                )
            ]
        )


@pytest.mark.asyncio
async def test_textual_app_can_start_and_show_configs(config_dir: Path) -> None:
    write_yaml(config_dir / "good.yaml", "name: good\nmodel: org/model")
    write_yaml(config_dir / "bad.yaml", "name: bad")

    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert "good" in app.config_summary
        assert "bad.yaml" in app.config_summary


@pytest.mark.asyncio
async def test_tui_loads_registry_and_preview_through_agent(config_dir: Path) -> None:
    agent = RecordingConfigAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.current_config is not None
        assert app.current_config.name == "alpha"
        assert "alpha" in app.config_summary
        assert "vllm serve org/alpha" in app.selected_config_preview
        assert agent.calls[:2] == [
            ("list_configs", {"configs_dir": str(config_dir)}),
            ("preview", {"name": "alpha", "configs_dir": str(config_dir)}),
        ]


@pytest.mark.asyncio
async def test_tui_select_config_refreshes_preview_through_agent(config_dir: Path) -> None:
    agent = RecordingConfigAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("beta")

        assert app.current_config is not None
        assert app.current_config.name == "beta"
        assert app.current_config.target == "blackbird"
        assert "vllm serve org/beta" in app.selected_config_preview
        assert agent.calls[-1] == ("preview", {"name": "beta", "configs_dir": str(config_dir)})


@pytest.mark.asyncio
async def test_tui_launch_preparation_runs_through_agent(config_dir: Path) -> None:
    agent = RecordingLaunchPrepareAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.MODEL_NOT_FOUND
        assert "agent-side missing model" in app.error_text
        assert agent.calls[-1] == (
            "prepare_launch",
            {"name": "alpha", "configs_dir": str(config_dir)},
        )


@pytest.mark.asyncio
async def test_tui_launch_fsm_uses_agent_profile_metadata(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AgentProfileLaunchAgent(RecordingConfigAgent):
        def handle(self, method: str, params: dict[str, str] | None = None):
            if method == "prepare_launch":
                self.calls.append((method, params))
                return {
                    "config": {"name": "alpha", "model": "org/alpha"},
                    "build": {
                        "argv": ["/bin/echo", "ready"],
                        "env": {},
                        "cwd": str(tmp_path),
                        "warnings": [],
                        "metadata": {"vllm_version_profile": "agent-profile"},
                        "preview": "",
                    },
                    "preflight": None,
                }
            return super().handle(method, params)

        def start_attached_run(self, *_args, **_kwargs):
            raise AssertionError("direct attached TUI start")

        async def wait_attached_run(self, *_args, **_kwargs):
            raise AssertionError("direct attached TUI wait")

        async def probe_run_until_ready(self, run_id: str, *, emit) -> None:
            emit(HealthEvent(ready=True, detail="ready from agent", models=["served"]))

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "probe_until_ready":
                return {
                    "run_id": "run-1",
                    "ready": True,
                    "detail": "ready from target client",
                    "models": ["served"],
                    "error_kind": None,
                }
            if method == "wait":
                return {"run_id": "run-1", "returncode": 0, "intentional": False}
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": 0,
                "intentional": False,
                "phase": Phase.STOPPED.value,
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            assert resume_from == "live"
            return self._events()

    def refuse_controller_profile(_cfg):
        raise AssertionError("TUI should use agent profile metadata")

    agent = AgentProfileLaunchAgent()
    monkeypatch.setattr(tui_app_module, "InProcessTargetClient", FakeTargetClient)
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)
    monkeypatch.setattr(
        tui_app_module,
        "select_profile_for_config",
        refuse_controller_profile,
        raising=False,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

        assert agent.calls[-1] == (
            "prepare_launch",
            {"name": "alpha", "configs_dir": str(config_dir)},
        )
        assert app.fsm.profile.version == "agent-profile"


@pytest.mark.asyncio
async def test_tui_attached_launch_uses_target_client_stream(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_instances: list[object] = []

    class AgentProfileLaunchAgent(RecordingConfigAgent):
        async def probe_run_until_ready(self, run_id: str, *, emit) -> None:
            emit(HealthEvent(ready=True, detail="ready", models=["served"]))

        def handle(self, method: str, params: dict[str, str] | None = None):
            if method == "prepare_launch":
                self.calls.append((method, params))
                return {
                    "config": {"name": "alpha", "model": "org/alpha"},
                    "build": {
                        "argv": ["/bin/echo", "ready"],
                        "env": {},
                        "cwd": str(tmp_path),
                        "warnings": [],
                        "metadata": {"vllm_version_profile": "agent-profile"},
                        "preview": "",
                    },
                    "preflight": None,
                }
            return super().handle(method, params)

        def start_attached_run(self, *_args, **_kwargs):
            raise AssertionError("direct attached TUI start")

        async def wait_attached_run(self, *_args, **_kwargs):
            raise AssertionError("direct attached TUI wait")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.connected = False
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "probe_until_ready":
                return {
                    "run_id": "run-1",
                    "ready": True,
                    "detail": "ready from target client",
                    "models": ["served"],
                    "error_kind": None,
                }
            if method == "wait":
                return {"run_id": "run-1", "returncode": 0, "intentional": False}
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            yield {
                "event": "log",
                "run_id": "run-1",
                "kind": "committed",
                "text": "INFO Starting to load model",
                "level": "INFO",
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }
            yield {
                "event": "phase",
                "run_id": "run-1",
                "phase": Phase.LOADING_WEIGHTS.value,
                "prev_phase": Phase.IDLE.value,
                "seq": 2,
                "ts": "2026-06-03T00:00:01Z",
                "mono": 2.0,
            }
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": 0,
                "intentional": False,
                "phase": Phase.ERROR.value,
                "seq": 3,
                "ts": "2026-06-03T00:00:02Z",
                "mono": 3.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            assert resume_from == "live"
            return self._events()

    monkeypatch.setattr(tui_app_module, "InProcessTargetClient", FakeTargetClient, raising=False)
    agent = AgentProfileLaunchAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()
        await pilot.pause()

        assert client_instances[0].calls == [
            ("launch", {"name": "alpha", "configs_dir": str(config_dir)}),
            ("probe_until_ready", {"run_id": "run-1"}),
            ("wait", {"run_id": "run-1"}),
        ]
        assert app.current_process is None
        assert app.current_run_id is None
        assert app.log_lines[-1] == "INFO Starting to load model"
        assert app.phase is Phase.ERROR


@pytest.mark.asyncio
async def test_tui_detached_launch_runs_through_agent(config_dir: Path, tmp_path: Path) -> None:
    class DetachedLaunchAgent(RecordingConfigAgent):
        def __init__(self) -> None:
            super().__init__()
            self.detached_launches: list[dict[str, object]] = []

        def handle(self, method: str, params: dict[str, str] | None = None):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/detached.yaml",
                            "name": "detached",
                            "model": "org/detached",
                            "target": None,
                            "warnings": [],
                            "config": {
                                "name": "detached",
                                "model": "org/detached",
                                "launch": {"mode": "detached"},
                            },
                        }
                    ],
                    "invalid": [],
                }
            if method == "prepare_launch":
                self.calls.append((method, params))
                return {
                    "config": {
                        "name": "detached",
                        "model": "org/detached",
                        "launch": {
                            "mode": "detached",
                            "runs_dir": str(tmp_path / "runs"),
                        },
                    },
                    "build": {
                        "argv": ["/bin/echo", "ready"],
                        "env": {},
                        "cwd": str(tmp_path),
                        "warnings": [],
                        "metadata": {"vllm_version_profile": "current"},
                        "preview": "",
                    },
                    "preflight": None,
                }
            return super().handle(method, params)

        def start_detached_run(self, prepared):
            self.detached_launches.append(prepared)
            return SimpleNamespace(sidecar_path=tmp_path / "runs" / "run-1.json")

    agent = DetachedLaunchAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)
    reattached: list[Path] = []
    app.reattach_detached_run = reattached.append

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

        assert len(agent.detached_launches) == 1
        assert reattached == [tmp_path / "runs" / "run-1.json"]


@pytest.mark.asyncio
async def test_command_palette_discovers_detached_runs_through_agent(
    config_dir: Path, tmp_path: Path
) -> None:
    sidecar_path = tmp_path / "runs" / "run-1.json"

    class DiscoveryAgent(RecordingConfigAgent):
        def __init__(self) -> None:
            super().__init__()
            self.discovered_dirs: list[list[Path]] = []

        def discover_detached_runs(self, runs_dirs):
            self.discovered_dirs.append(list(runs_dirs))
            return [
                SimpleNamespace(
                    run_id="run-1",
                    sidecar_path=sidecar_path,
                    config_name="detached",
                )
            ]

    agent = DiscoveryAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)
    reattached: list[Path] = []
    app.reattach_detached_run = reattached.append

    async with app.run_test() as pilot:
        await pilot.pause()

        commands = list(app.get_system_commands(app.screen))
        command = next(
            item for item in commands if item.title == "Reattach detached run: detached"
        )
        command.callback()

        assert agent.discovered_dirs == [app._runs_dirs()]
        assert reattached == [sidecar_path]


@pytest.mark.asyncio
async def test_tui_stop_attached_run_signals_target_client_by_run_id(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RunningProcess:
        def __init__(self) -> None:
            self.proc = self
            self.stopped = False

        def poll(self) -> None:
            return None

        def stop(self, *args, **kwargs) -> None:
            self.stopped = True

    class StopRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            return run_id == "run-1"

        def stop_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct attached TUI stop")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "stop":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("stop should not subscribe")

    monkeypatch.setattr(tui_app_module, "InProcessTargetClient", FakeTargetClient)
    agent = StopRefusingAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        fake_process = RunningProcess()
        app.current_process = fake_process
        app.current_run_id = "run-1"

        app.action_stop()
        await _wait_for_condition(
            lambda: app._target_client.calls
            == [
                (
                    "stop",
                    {
                        "run_id": "run-1",
                        "interrupt_timeout": 2,
                        "terminate_timeout": 2,
                    },
                )
            ],
            "target client stop was not requested",
        )

        assert fake_process.stopped is False


@pytest.mark.asyncio
async def test_tui_attached_health_probe_runs_through_target_client(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProbeRefusingAgent(StopRecordingAgent):
        async def probe_run_until_ready(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct attached TUI probe")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "probe_until_ready":
                return {
                    "run_id": params["run_id"],
                    "ready": True,
                    "detail": "ready from target client",
                    "models": ["served"],
                    "error_kind": None,
                }
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("direct probe should not subscribe")

    monkeypatch.setattr(tui_app_module, "InProcessTargetClient", FakeTargetClient)
    agent = ProbeRefusingAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_config is not None
        app.current_run_id = "run-1"

        await app._probe_until_ready(app.current_config)
        await pilot.pause()

        assert app._target_client.calls == [
            ("probe_until_ready", {"run_id": "run-1"})
        ]
        assert app.phase is Phase.READY
        assert app.served_models == ["served"]


@pytest.mark.asyncio
async def test_tui_detached_health_probe_runs_through_agent(
    config_dir: Path, tmp_path: Path
) -> None:
    sidecar_path = tmp_path / "detached.json"
    agent = HealthProbeRecordingAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_config is not None
        app.reattached_sidecar_path = sidecar_path
        app.reattached_run_id = "run-1"

        await app._probe_detached_until_ready(app.current_config, sidecar_path)
        await pilot.pause()

        assert agent.probe_calls == ["run-1"]
        assert app.phase is Phase.READY
        assert app.served_models == ["served"]


@pytest.mark.asyncio
async def test_tui_consumes_attached_run_events_from_agent(config_dir: Path) -> None:
    class EventEmittingAgent(RecordingConfigAgent):
        def start_attached_run(self, prepared, *, emit_event):
            emit_event(
                SimpleNamespace(
                    kind="log",
                    run_id="run-1",
                    payload={
                        "kind": "committed",
                        "text": "INFO Starting to load model",
                        "level": "INFO",
                    },
                )
            )
            emit_event(
                SimpleNamespace(
                    kind="phase",
                    run_id="run-1",
                    payload={"phase": Phase.LOADING_WEIGHTS.value},
                )
            )
            return SimpleNamespace(run_id="run-1", process=SimpleNamespace(proc=None))

    agent = EventEmittingAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()

        app._agent_start_attached_run({})
        await pilot.pause()

        assert app.log_lines[-1] == "INFO Starting to load model"
        assert app.phase is Phase.LOADING_WEIGHTS


@pytest.mark.asyncio
async def test_tui_gpu_sampling_runs_through_agent(config_dir: Path) -> None:
    agent = GpuRecordingAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent, gpu_interval_seconds=60)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._sample_gpu_panel_once()
        await pilot.pause()

        assert agent.sample_calls >= 1
        assert "A100" in app.gpu_panel_text


@pytest.mark.asyncio
async def test_help_screen_opens(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("?")
        await pilot.pause()
        assert app.screen.id == "help"
        assert isinstance(app.screen, ModalScreen)
        help_text = app.screen.query_one("#help-text", Static)
        assert help_text.region.x > 0
        assert help_text.region.y > 0
        assert "Tab focus" in str(help_text.content)
        assert isinstance(help_text.content, Text)
        assert _text_uses_style(help_text.content, tui_app_module.ACCENT)
        assert _text_uses_style(help_text.content, tui_app_module.GOOD)


@pytest.mark.asyncio
async def test_prompt_and_picker_screens_render_as_modal_panels(config_dir: Path) -> None:
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ModalScreen)
        assert app.screen.query_one("#config-picker-panel").region.x > 0

        await pilot.press("escape")
        await pilot.press("/")
        await pilot.pause()
        assert isinstance(app.screen, ModalScreen)
        assert app.screen.query_one("#log-prompt-panel").region.x > 0


@pytest.mark.asyncio
async def test_config_picker_marks_invalid_configs_with_warning_glyph(
    config_dir: Path,
) -> None:
    write_yaml(config_dir / "good.yaml", "name: good\nmodel: org/model")
    write_yaml(config_dir / "bad.yaml", "name: bad")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()

        assert "⚠ bad.yaml:" in app.screen.summary
        assert "! bad.yaml:" not in app.screen.summary


@pytest.mark.asyncio
async def test_invalid_config_surfaces_all_field_errors(config_dir: Path) -> None:
    write_yaml(config_dir / "good.yaml", "name: good\nmodel: org/model")
    write_yaml(
        config_dir / "bad.yaml",
        """
        name: bad
        model: org/model
        engine:
          dtype: nope
        server:
          host: 0.0.0.0
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        dashboard_configs = _static_text(app, "#configs")

        assert "engine.dtype:" in dashboard_configs
        assert "server:" in dashboard_configs

        await pilot.press("c")
        await pilot.pause()

        assert "engine.dtype:" in app.screen.summary
        assert "server:" in app.screen.summary


@pytest.mark.asyncio
async def test_confirm_screen_is_modal_panel_with_destructive_color(
    config_dir: Path,
) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ConfirmScreen("Attached server is still running. Stop it?"))
        await pilot.pause()

        assert app.screen.id == "confirm"
        assert isinstance(app.screen, ModalScreen)
        assert app.screen.query_one("#confirm-panel").region.x > 0
        message = app.screen.query_one("#confirm-message", Static)
        assert isinstance(message.content, Text)
        assert "Stop" in message.content.plain
        assert "Cancel" in message.content.plain
        assert _text_uses_style(message.content, tui_app_module.BAD)
        assert _text_uses_style(message.content, tui_app_module.GOOD)


@pytest.mark.asyncio
async def test_kill_while_attached_running_prompts_before_signal(
    config_dir: Path,
) -> None:
    class RunningProcess:
        def __init__(self) -> None:
            self.proc = self
            self.killed = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

    fake_process = RunningProcess()
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.current_process = fake_process

        await pilot.press("K")
        await pilot.pause()

        assert app.screen.id == "confirm"
        message = app.screen.query_one("#confirm-message", Static)
        assert isinstance(message.content, Text)
        assert "Kill" in message.content.plain
        assert not fake_process.killed

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen.id != "confirm"
        assert not fake_process.killed

        await pilot.press("K")
        await pilot.press("enter")
        await pilot.pause()

        assert fake_process.killed
        assert app.screen.id != "confirm"


@pytest.mark.asyncio
async def test_confirm_kill_attached_run_signals_target_client_by_run_id(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RunningProcess:
        def __init__(self) -> None:
            self.proc = self
            self.killed = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

    class KillRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            return run_id == "run-1"

        def kill_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct attached TUI kill")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "kill":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("kill should not subscribe")

    monkeypatch.setattr(tui_app_module, "InProcessTargetClient", FakeTargetClient)
    fake_process = RunningProcess()
    app = VllmLoaderApp(configs_dir=config_dir, agent=KillRefusingAgent())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.current_process = fake_process
        app.current_run_id = "run-1"

        await pilot.press("K")
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: app._target_client.calls == [("kill", {"run_id": "run-1"})],
            "target client kill was not requested",
        )

        assert fake_process.killed is False
        assert app.screen.id != "confirm"


@pytest.mark.asyncio
async def test_config_picker_displays_valid_invalid_and_selects_config(config_dir: Path) -> None:
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    write_yaml(config_dir / "beta.yaml", "name: beta\nmodel: org/beta")
    write_yaml(config_dir / "broken.yaml", "name: broken")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.id == "config-picker"
        assert "alpha" in app.screen.summary
        assert "beta" in app.screen.summary
        assert "broken.yaml" in app.screen.summary
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert app.current_config is not None
        assert app.current_config.name == "beta"


@pytest.mark.asyncio
async def test_config_picker_filters_configs_and_selects_filtered_match(
    config_dir: Path,
) -> None:
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    write_yaml(config_dir / "beta.yaml", "name: beta\nmodel: org/beta")
    write_yaml(config_dir / "gamma.yaml", "name: gamma\nmodel: org/gamma")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("c")
        await pilot.press("g", "a", "m")
        await pilot.pause()

        assert "gamma" in app.screen.summary
        assert "alpha" not in app.screen.summary
        assert "beta" not in app.screen.summary

        await pilot.press("enter")
        await pilot.pause()

        assert app.current_config is not None
        assert app.current_config.name == "gamma"


@pytest.mark.asyncio
async def test_config_picker_shows_masked_preview_for_selected_config(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "preview.yaml",
        """
        name: preview
        model: org/secret-model
        server:
          host: 127.0.0.1
          port: 8123
          api_key: sk-live-secret
        env:
          HF_TOKEN: hf_private_token
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()

        assert "Resolved command" in app.screen.summary
        assert "vllm serve org/secret-model" in app.screen.summary
        assert "--port 8123" in app.screen.summary
        assert "VLLM_API_KEY='••••'" in app.screen.summary
        assert "HF_TOKEN='••••'" in app.screen.summary
        assert "sk-live-secret" not in app.screen.summary
        assert "hf_private_token" not in app.screen.summary


@pytest.mark.asyncio
async def test_selected_config_preview_masks_secrets_before_launch(config_dir: Path) -> None:
    write_yaml(
        config_dir / "preview.yaml",
        """
        name: preview
        model: org/secret-model
        server:
          host: 127.0.0.1
          port: 8123
          api_key: sk-live-secret
        env:
          HF_TOKEN: hf_private_token
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("preview")

        assert "cwd=" in app.selected_config_preview
        assert "vllm serve org/secret-model" in app.selected_config_preview
        assert "--port 8123" in app.selected_config_preview
        assert "VLLM_API_KEY='••••'" in app.selected_config_preview
        assert "HF_TOKEN='••••'" in app.selected_config_preview
        assert "sk-live-secret" not in app.selected_config_preview
        assert "hf_private_token" not in app.selected_config_preview
        assert "Selected: preview" in app.config_summary
        assert "Model: org/secret-model" in app.config_summary
        assert "Server: http://127.0.0.1:8123" in app.config_summary
        assert "Full preview: press c" in app.config_summary
        assert app.selected_config_preview not in app.config_summary
        assert "Resolved command" not in app.config_summary
        assert "cwd=" not in app.config_summary


@pytest.mark.asyncio
async def test_selected_config_preview_uses_config_executable_help_for_require_flags(
    config_dir: Path,
    tmp_path: Path,
) -> None:
    fake_vllm = tmp_path / "fake-vllm"
    fake_vllm.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('vllm 0.11.2')\n"
        "elif sys.argv[1:] == ['serve', '--help']:\n"
        "    print('usage: vllm serve')\n"
        "    print('  --host TEXT')\n"
        "    print('  --port INTEGER')\n",
        encoding="utf-8",
    )
    fake_vllm.chmod(0o755)
    write_yaml(
        config_dir / "custom-help.yaml",
        f"""
        name: custom-help
        model: org/model
        command:
          entrypoint: serve
          executable: {fake_vllm}
        vllm:
          require_flags:
            - --disable-log-requests
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert "Preview unavailable" in app.selected_config_preview
        assert "--disable-log-requests" in app.selected_config_preview


@pytest.mark.asyncio
async def test_select_config_with_profile_gate_failure_shows_preview_error(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "unsupported-preview.yaml",
        """
        name: unsupported-preview
        model: org/model
        vllm:
          require_flags:
            - --definitely-missing-flag
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("unsupported-preview")

        assert app.current_config is not None
        assert app.current_config.name == "unsupported-preview"
        assert "Preview unavailable" in app.selected_config_preview
        assert "--definitely-missing-flag" in app.selected_config_preview
        assert "Selected: unsupported-preview" in app.config_summary
        assert "Full preview: press c" in app.config_summary
        assert "Traceback" not in app.error_text


@pytest.mark.asyncio
async def test_phase_timeline_tracks_elapsed_time(config_dir: Path) -> None:
    now = 100.0

    def clock() -> float:
        return now

    app = VllmLoaderApp(configs_dir=config_dir, clock=clock)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_phase(Phase.STARTING)
        now = 102.0
        app._set_phase(Phase.LOADING_WEIGHTS)
        now = 105.0
        app._set_phase(Phase.READY)
        now = 107.0
        app._set_phase(Phase.READY)
        assert "✓ STARTING 00:02" in app.phase_timeline_text
        assert "✓ LOADING_WEIGHTS 00:03" in app.phase_timeline_text
        assert "● READY 00:02" in app.phase_timeline_text
        assert "Overall 00:07" in app.phase_timeline_text


@pytest.mark.asyncio
async def test_status_badge_uses_icons_and_phase_color_classes(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        status = app.query_one("#status", Static)

        assert app.status_text == "○ IDLE"
        assert status.has_class("status--idle")

        app._set_phase(Phase.STARTING)
        assert app.status_text == "● STARTING"
        assert status.has_class("status--loading")
        assert status.has_class("status--pulse")

        app._set_phase(Phase.READY)
        assert app.status_text.startswith("● READY")
        assert status.has_class("status--ready")
        assert not status.has_class("status--pulse")

        app._set_phase(Phase.ERROR)
        assert app.status_text == "✕ ERROR"
        assert status.has_class("status--error")


@pytest.mark.asyncio
async def test_dashboard_uses_figma_terminal_shell_chrome_and_footer(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "llama.yaml",
        """
        name: llama-3.1-70b-awq
        model: meta-llama/Llama-3.1-70B-Instruct-AWQ
        server:
          host: 127.0.0.1
          port: 8000
        engine:
          tensor_parallel_size: 4
          kv_cache_dtype: fp8
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test(size=(144, 45)) as pilot:
        await pilot.pause()

        for selector in [
            "#terminal-shell",
            "#top-chrome",
            "#app-title",
            "#active-model",
            "#server-url",
            "#log-panel",
            "#log-title",
            "#log-controls",
            "#status-strip",
            "#footer-bindings",
        ]:
            app.query_one(selector)

        assert _static_text(app, "#app-title") == "vLLM Loader"
        assert "llama-3.1-70b-awq" in _static_text(app, "#active-model")
        assert "http://127.0.0.1:8000" in _static_text(app, "#server-url")
        assert "Logs - unified child stdout/stderr stream" in _static_text(app, "#log-title")
        assert "autoscroll ON" in _static_text(app, "#log-controls")
        assert "wrap OFF" in _static_text(app, "#log-controls")
        assert "lines" in _static_text(app, "#status-strip")
        assert "scrubbed log 0600" in _static_text(app, "#status-strip")
        assert "l Load" in _static_text(app, "#footer-bindings")
        assert "Tab Focus" in _static_text(app, "#footer-bindings")
        assert "^P Palette" in _static_text(app, "#footer-bindings")

        status = app.query_one("#status")
        status_strip = app.query_one("#status-strip")
        footer = app.query_one("#footer-bindings")
        assert status.region.height >= 3
        assert status_strip.region.y + status_strip.region.height <= footer.region.y


@pytest.mark.asyncio
async def test_dashboard_status_strip_tracks_log_controls(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._write_log("INFO operator note", "INFO")
        await pilot.press("p")
        await pilot.press("w")
        await pilot.pause()

        assert "autoscroll OFF" in _static_text(app, "#log-controls")
        assert "wrap ON" in _static_text(app, "#log-controls")
        assert f"{len(app.log_lines)} lines" in _static_text(app, "#status-strip")
        assert "autoscroll OFF" in _static_text(app, "#status-strip")
        assert "wrap ON" in _static_text(app, "#status-strip")


@pytest.mark.asyncio
async def test_wrap_toggle_notifies_state_change(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)
    notifications: list[str] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, *args, **kwargs: notifications.append(message),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()

        assert app.wrap is True
        assert notifications[-1] == "Wrap enabled"

        await pilot.press("w")
        await pilot.pause()

        assert app.wrap is False
        assert notifications[-1] == "Wrap disabled"


@pytest.mark.asyncio
async def test_dashboard_uses_intentional_rich_color_renderables(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_phase(Phase.STARTING)
        app._render_gpu_panel(
            GpuPollResult(
                [
                    GpuSample(
                        visible_index=0,
                        uuid="GPU-a",
                        name="A100",
                        memory_used_mb=2048,
                        memory_total_mb=81920,
                        utilization_percent=55,
                        temperature_c=44,
                        power_w=120,
                    )
                ]
            )
        )

        status_content = app.query_one("#status", Static).content
        controls_content = app.query_one("#log-controls", Static).content
        strip_content = app.query_one("#status-strip", Static).content
        gpu_content = app.query_one("#gpu", Static).content
        warning_line = app._make_log_text("WARNING Access log disabled", "WARNING")

        assert isinstance(status_content, Text)
        assert isinstance(controls_content, Text)
        assert isinstance(strip_content, Text)
        assert isinstance(gpu_content, Text)
        assert warning_line.plain.startswith("▌ ")
        assert "▰" in gpu_content.plain
        for renderable in [
            status_content,
            controls_content,
            strip_content,
            gpu_content,
            warning_line,
        ]:
            assert renderable.style or renderable.spans


@pytest.mark.asyncio
async def test_sidebar_and_banner_use_semantic_color_roles(config_dir: Path) -> None:
    write_yaml(
        config_dir / "llama.yaml",
        """
        name: llama-3.1-70b-awq
        model: meta-llama/Llama-3.1-70B-Instruct-AWQ
        engine:
          tensor_parallel_size: 4
          kv_cache_dtype: fp8
        """,
    )
    write_yaml(config_dir / "broken.yaml", "name: broken")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_phase(Phase.STARTING)
        app._set_phase(Phase.LOADING_WEIGHTS)
        app._record_warnings(["0.0.0.0 bind requires an API key"])

        configs_title = app.query_one("#configs-title", Static).content
        configs = app.query_one("#configs", Static).content
        phases = app.query_one("#phases", Static).content
        warning = app.query_one("#error", Static).content

        assert isinstance(configs_title, Text)
        assert isinstance(configs, Text)
        assert isinstance(phases, Text)
        assert isinstance(warning, Text)
        assert "llama-3.1-70b-awq" in configs.plain
        assert "broken.yaml" in configs.plain
        assert "✓ STARTING" in phases.plain
        assert "● LOADING_WEIGHTS" in phases.plain
        assert _text_uses_style(configs_title, tui_app_module.ACCENT)
        assert _text_uses_style(configs, tui_app_module.ACCENT)
        assert _text_uses_style(configs, tui_app_module.WARN)
        assert _text_uses_style(configs, tui_app_module.MUTED)
        assert _text_uses_style(phases, tui_app_module.GOOD)
        assert _text_uses_style(phases, tui_app_module.WARN)
        assert _text_uses_style(phases, tui_app_module.MUTED)
        assert _text_uses_style(warning, tui_app_module.WARN)

        app._set_error_text("fatal launch failure")
        error = app.query_one("#error", Static).content
        assert isinstance(error, Text)
        assert _text_uses_style(error, tui_app_module.BAD)


@pytest.mark.asyncio
async def test_terminal_phases_clear_stale_progress_line(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._update_progress("Loading safetensors checkpoint shards: 100% 4/4")
        assert app.progress_text

        app._set_phase(Phase.READY)

        assert app.progress_text == ""
        assert app.query_one("#progress-line").display is False


@pytest.mark.asyncio
async def test_tui_consumes_canonical_textual_messages(config_dir: Path) -> None:
    write_yaml(
        config_dir / "messages.yaml",
        """
        name: messages
        model: org/model
        server:
          host: 127.0.0.1
          port: 8126
        """,
    )
    app = VllmLoaderApp(
        configs_dir=config_dir,
        gpu_sampler=lambda: GpuPollResult([]),
        gpu_interval_seconds=60,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("messages")

        app.post_message(ProgressUpdated("Loading shards: 25% 1/4"))
        await pilot.pause()
        assert "25%" in app.progress_text

        app.post_message(LogLineCommitted("INFO Uvicorn running on http://127.0.0.1:8126", "INFO"))
        await pilot.pause()
        assert any("Uvicorn running" in line for line in app.log_lines)

        app.post_message(PhaseChanged(Phase.LOADING_WEIGHTS))
        await pilot.pause()
        assert app.phase is Phase.LOADING_WEIGHTS

        app.post_message(ServerReady(["served"]))
        await pilot.pause()
        assert app.phase is Phase.READY
        assert app.served_models == ["served"]
        assert app.ready_url == "http://127.0.0.1:8126"

        app.post_message(HealthChanged(ready=False, detail="health returned 503"))
        await pilot.pause()
        assert app.phase is Phase.DEGRADED

        app.post_message(
            GpuStatsUpdated(
                GpuPollResult(
                    [
                        GpuSample(
                            visible_index=0,
                            uuid="GPU-a",
                            name="A100",
                            memory_used_mb=2048,
                            memory_total_mb=81920,
                            utilization_percent=55,
                        )
                    ]
                )
            )
        )
        await pilot.pause()
        assert "2048/81920MB" in app.gpu_panel_text

        app.post_message(GpuStatsUnavailable("GPU stats unavailable: no nvml"))
        await pilot.pause()
        assert app.gpu_panel_text == "GPU stats unavailable: no nvml"

        app.post_message(ProcessExited(0))
        await pilot.pause()
        assert app.phase is Phase.STOPPED

        app.post_message(EngineError(ErrorKind.OOM, "CUDA out of memory"))
        await pilot.pause()
        assert app.fsm.error_kind is ErrorKind.OOM
        assert "OOM" in app.error_text


def test_late_log_message_updates_state_when_widgets_are_unmounted(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    app.on_log_line_committed(LogLineCommitted("INFO Starting to load model", "INFO"))

    assert app.phase is Phase.LOADING_WEIGHTS
    assert app.status_text == "● LOADING_WEIGHTS"
    assert app.log_lines == ["INFO Starting to load model"]


@pytest.mark.asyncio
async def test_gpu_panel_refreshes_periodically(config_dir: Path) -> None:
    results = [
        GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="A100",
                    memory_used_mb=1024,
                    memory_total_mb=81920,
                    utilization_percent=25,
                    temperature_c=42,
                    power_w=110,
                )
            ]
        ),
        GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="A100",
                    memory_used_mb=2048,
                    memory_total_mb=81920,
                    utilization_percent=55,
                    temperature_c=44,
                    power_w=120,
                )
            ]
        ),
    ]
    calls = 0

    def sampler() -> GpuPollResult:
        nonlocal calls
        result = results[min(calls, len(results) - 1)]
        calls += 1
        return result

    app = VllmLoaderApp(configs_dir=config_dir, gpu_sampler=sampler, gpu_interval_seconds=0.05)

    async with app.run_test() as pilot:
        await _wait_for_gpu_text(app, "2048/81920MB")
        await pilot.pause()
        assert calls >= 2
        assert "55%" in app.gpu_panel_text
        assert "44C" in app.gpu_panel_text
        assert "120W" in app.gpu_panel_text


@pytest.mark.asyncio
async def test_gpu_sampler_runs_off_event_loop_thread(config_dir: Path) -> None:
    event_loop_thread = threading.get_ident()
    sampler_threads: list[int] = []

    def sampler() -> GpuPollResult:
        sampler_threads.append(threading.get_ident())
        return GpuPollResult([])

    app = VllmLoaderApp(configs_dir=config_dir, gpu_sampler=sampler, gpu_interval_seconds=0.01)

    async with app.run_test() as pilot:
        await _wait_for_gpu_calls(sampler_threads, 2)
        await pilot.pause()

    assert any(thread_id != event_loop_thread for thread_id in sampler_threads)


@pytest.mark.asyncio
async def test_gpu_sampler_error_renders_unavailable_detail(config_dir: Path) -> None:
    calls = 0

    def sampler() -> GpuPollResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("nvml exploded")

    app = VllmLoaderApp(configs_dir=config_dir, gpu_sampler=sampler, gpu_interval_seconds=60)

    async with app.run_test() as pilot:
        await _wait_for_gpu_text(app, "GPU stats unavailable: nvml exploded")
        await pilot.pause()

        assert calls >= 1
        gpu_content = app.query_one("#gpu", Static).content
        assert isinstance(gpu_content, Text)
        assert "GPU stats unavailable: nvml exploded" in gpu_content.plain


@pytest.mark.asyncio
async def test_optional_monitor_worker_errors_notify_operator(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)
    notifications: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, *args, **kwargs: notifications.append(
            (message, kwargs.get("severity"))
        ),
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        app.on_worker_state_changed(
            SimpleNamespace(
                state=WorkerState.ERROR,
                worker=SimpleNamespace(group="health", error=RuntimeError("probe failed")),
            )
        )
        app.on_worker_state_changed(
            SimpleNamespace(
                state=WorkerState.ERROR,
                worker=SimpleNamespace(group="monitoring", error=RuntimeError("nvml failed")),
            )
        )

        assert notifications == [
            ("health monitor stopped: probe failed", "warning"),
            ("gpu monitor stopped: nvml failed", "warning"),
        ]


@pytest.mark.asyncio
async def test_classified_log_error_shows_named_banner_with_suggestion(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.handle_log_record(LogRecord("committed", "ERROR CUDA out of memory", "ERROR"))

        assert app.phase is Phase.ERROR
        assert "OOM" in app.error_text
        assert "CUDA out of memory" in app.error_text
        assert "gpu_memory_utilization" in app.error_text
        assert "max_model_len" in app.error_text
        assert "Jump to error log line" in app.error_text

        jump = next(
            command
            for command in app.get_system_commands(app.screen)
            if command.title == "Jump to error log line"
        )
        jump.callback()

        assert app.search_text == "CUDA out of memory"
        assert app.search_matches == ["ERROR CUDA out of memory"]


@pytest.mark.asyncio
async def test_health_error_shows_named_banner_with_suggestion(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._handle_health_event(
            HealthEvent(
                ready=False,
                detail="Bearer token mismatch for /v1/models; check VLLM_API_KEY/api_key",
                error_kind=ErrorKind.HF_AUTH,
            )
        )

        assert app.phase is Phase.ERROR
        assert "HF_AUTH" in app.error_text
        assert "HF_TOKEN" in app.error_text
        assert "Bearer token mismatch" in app.error_text


@pytest.mark.asyncio
async def test_nonzero_exit_before_ready_shows_crashed_banner(
    config_dir: Path, tmp_path: Path
) -> None:
    port = _free_port()
    script = tmp_path / "crashing_child.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "print('ERROR synthetic loader abort before ready', flush=True)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    write_yaml(
        config_dir / "crash.yaml",
        f"""
        name: crash
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("l")
        await _wait_for_phase(app, Phase.ERROR)

        assert app.fsm.error_kind is ErrorKind.CRASHED
        assert "CRASHED" in app.error_text
        assert "synthetic loader abort before ready" in app.error_text


@pytest.mark.asyncio
async def test_nonzero_exit_without_logs_shows_exit_code_excerpt(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(ProcessExited(7))
        await pilot.pause()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.CRASHED
        assert "CRASHED" in app.error_text
        assert "process exited with code 7" in app.error_text


@pytest.mark.asyncio
async def test_missing_executable_shows_launch_guidance_instead_of_crashing(
    config_dir: Path, tmp_path: Path
) -> None:
    missing_executable = tmp_path / "does-not-exist"
    write_yaml(
        config_dir / "missing-bin.yaml",
        f"""
        name: missing-bin
        model: fake/model
        command:
          entrypoint: serve
          executable: {missing_executable}
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = app.registry.by_name("missing-bin")
        await app._run_selected_config()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.COMMAND_NOT_FOUND
        assert "COMMAND_NOT_FOUND" in app.error_text
        assert "install vLLM" in app.error_text
        assert "command.entrypoint: module" in app.error_text


@pytest.mark.asyncio
async def test_missing_local_model_path_shows_model_not_found_without_launching(
    config_dir: Path, tmp_path: Path
) -> None:
    missing_model = tmp_path / "missing-model"
    script = tmp_path / "should_not_run.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('INFO script should not run', flush=True)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    write_yaml(
        config_dir / "missing-local-model.yaml",
        f"""
        name: missing-local-model
        model: {missing_model}
        command:
          entrypoint: serve
          executable: {script}
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = app.registry.by_name("missing-local-model")
        await app._run_selected_config()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.MODEL_NOT_FOUND
        assert "MODEL_NOT_FOUND" in app.error_text
        assert str(missing_model) in app.error_text
        assert not any("script should not run" in line for line in app.log_lines)


@pytest.mark.asyncio
async def test_unsupported_required_flag_shows_config_error_without_launching(
    config_dir: Path, tmp_path: Path
) -> None:
    script = tmp_path / "should_not_run.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "print('INFO script should not run', flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    write_yaml(
        config_dir / "unsupported-required-flag.yaml",
        f"""
        name: unsupported-required-flag
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        vllm:
          require_flags:
            - --definitely-missing-flag
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = app.registry.by_name("unsupported-required-flag")
        await app._run_selected_config()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is not None
        assert app.fsm.error_kind.value == "CONFIG_INVALID"
        assert "CONFIG_INVALID" in app.error_text
        assert "required vLLM flags are unavailable" in app.error_text
        assert "--definitely-missing-flag" in app.error_text
        assert not any("script should not run" in line for line in app.log_lines)


@pytest.mark.asyncio
async def test_parallel_world_size_exceeding_visible_gpus_shows_tp_mismatch_without_launching(
    config_dir: Path, tmp_path: Path
) -> None:
    script = tmp_path / "should_not_run.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "print('INFO script should not run', flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    write_yaml(
        config_dir / "too-many-gpus.yaml",
        f"""
        name: too-many-gpus
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        engine:
          tensor_parallel_size: 4
          pipeline_parallel_size: 1
        env:
          CUDA_VISIBLE_DEVICES: "0,1"
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = app.registry.by_name("too-many-gpus")
        await app._run_selected_config()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.TP_MISMATCH
        assert "TP_MISMATCH" in app.error_text
        assert "world size 4" in app.error_text
        assert "2 visible GPUs" in app.error_text
        assert not any("script should not run" in line for line in app.log_lines)


@pytest.mark.asyncio
async def test_occupied_port_shows_port_in_use_without_launching(
    config_dir: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "child-started.txt"
    script = tmp_path / "should_not_run.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('started', encoding='utf-8')\n"
        "print('INFO script should not run', flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        write_yaml(
            config_dir / "port-in-use.yaml",
            f"""
            name: port-in-use
            model: fake/model
            command:
              entrypoint: serve
              executable: {script}
            server:
              host: 127.0.0.1
              port: {port}
            """,
        )
        app = VllmLoaderApp(configs_dir=config_dir)

        async with app.run_test() as pilot:
            await pilot.pause()
            app.current_config = app.registry.by_name("port-in-use")
            await app._run_selected_config()

            assert app.phase is Phase.ERROR
            assert app.fsm.error_kind is ErrorKind.PORT_IN_USE
            assert "PORT_IN_USE" in app.error_text
            assert str(port) in app.error_text
            assert not marker.exists()
            assert not any("script should not run" in line for line in app.log_lines)


@pytest.mark.asyncio
async def test_detached_missing_executable_shows_launch_guidance(
    config_dir: Path, tmp_path: Path
) -> None:
    missing_executable = tmp_path / "detached-missing"
    runs_dir = tmp_path / "runs"
    write_yaml(
        config_dir / "detached-missing-bin.yaml",
        f"""
        name: detached-missing-bin
        model: fake/model
        command:
          entrypoint: serve
          executable: {missing_executable}
        launch:
          mode: detached
          runs_dir: {runs_dir}
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = app.registry.by_name("detached-missing-bin")
        await app._run_selected_config()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.COMMAND_NOT_FOUND
        assert "COMMAND_NOT_FOUND" in app.error_text
        assert "install vLLM" in app.error_text
        assert not list(runs_dir.glob("*.json"))


@pytest.mark.asyncio
async def test_command_palette_exposes_core_actions_and_config_loads(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    write_yaml(config_dir / "beta.yaml", "name: beta\nmodel: org/beta")
    app = VllmLoaderApp(configs_dir=config_dir)
    load_calls: list[str | None] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app,
            "action_load",
            lambda: load_calls.append(
                app.current_config.name if app.current_config is not None else None
            ),
        )
        commands = list(app.get_system_commands(app.screen))
        titles = {command.title for command in commands}
        assert {
            "Load selected config",
            "Stop server",
            "Force kill server",
            "Restart server",
            "Open config picker",
            "Search logs",
            "Filter logs",
            "Toggle autoscroll",
            "Toggle wrap",
            "Scroll logs to top",
            "Scroll logs to bottom",
            "Open help",
            "Copy server URL",
            "Focus next widget",
            "Quit app",
            "Load config: beta",
        } <= titles

        beta = next(command for command in commands if command.title == "Load config: beta")
        beta.callback()
        assert app.current_config is not None
        assert app.current_config.name == "beta"
        assert load_calls == ["beta"]


@pytest.mark.asyncio
async def test_command_palette_reattaches_detached_run(config_dir: Path, tmp_path: Path) -> None:
    port = _free_port()
    runs_dir = tmp_path / "runs"
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "detached.yaml",
        f"""
        name: detached
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          mode: detached
          runs_dir: {runs_dir}
          health:
            interval_seconds: 0.05
        """,
    )
    cfg = load_registry(config_dir).by_name("detached")
    launch = start_detached(cfg, build_command(cfg), secrets=[])
    await _wait_for_log_text(launch.log_path, "Uvicorn running")

    app = VllmLoaderApp(configs_dir=config_dir)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            reattach = await _wait_for_command(
                app, "Reattach detached run: detached"
            )
            reattach.callback()
            await _wait_for_phase(app, Phase.READY)
            assert app.reattached_sidecar_path == launch.sidecar_path
            assert any("Uvicorn running" in line for line in app.log_lines)
    finally:
        await _cleanup_port(port)


def test_reattach_discovery_scans_default_and_configured_runs_dirs(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    custom_runs_dir = tmp_path / "custom-runs"
    write_yaml(
        config_dir / "custom-runs.yaml",
        f"""
        name: custom-runs
        model: org/model
        launch:
          runs_dir: {custom_runs_dir}
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)
    app.registry = load_registry(config_dir)

    assert app._runs_dirs() == sorted(
        [home / ".local" / "state" / "vllm-loader" / "runs", custom_runs_dir]
    )


@pytest.mark.asyncio
async def test_load_while_reattached_refuses_second_managed_run(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "attached.yaml",
        """
        name: attached
        model: fake/model
        command:
          entrypoint: serve
          executable: ./scripts/fake_vllm_child.py
        server:
          host: 127.0.0.1
          port: 8765
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)
    notifications: list[str] = []
    worker_calls: list[str] = []

    def capture_worker(coro, **kwargs):
        worker_calls.append(kwargs.get("name", ""))
        coro.close()

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app,
            "notify",
            lambda message, *args, **kwargs: notifications.append(message),
        )
        monkeypatch.setattr(app, "run_worker", capture_worker)
        app.reattached_sidecar_path = tmp_path / "detached.json"
        app.action_load()

        assert worker_calls == []
        assert notifications[-1] == "A detached run is already attached"


@pytest.mark.asyncio
async def test_stop_after_reattach_cancels_detached_monitor_workers(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar_path = tmp_path / "detached.json"
    stopped_paths: list[Path] = []
    cancelled_groups: list[str] = []

    def stop_sidecar(path: Path, **_kwargs: object) -> None:
        stopped_paths.append(path)

    def cancel_group(_app: VllmLoaderApp, group: str) -> None:
        cancelled_groups.append(group)

    monkeypatch.setattr(tui_app_module, "stop_sidecar_from_system", stop_sidecar)
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app.workers, "cancel_group", cancel_group)
        app.reattached_sidecar_path = sidecar_path
        app._set_phase(Phase.READY)

        app.action_stop()

        assert stopped_paths == [sidecar_path]
        assert cancelled_groups == ["tail", "health"]
        assert app.reattached_sidecar_path is None
        assert app.phase is Phase.STOPPED


@pytest.mark.asyncio
async def test_stop_after_agent_reattach_signals_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar_path = tmp_path / "detached.json"
    log_path = tmp_path / "detached.log"
    log_path.write_text("", encoding="utf-8")
    manifest = Manifest.from_active_log(log_path)
    sidecar = Sidecar(
        run_id="run-1",
        config_name="detached",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=[],
        exposure="local",
        manifest_path=str(tmp_path / "detached.manifest.json"),
        config_snapshot={"name": "detached", "model": "fake/model"},
    )

    class ReattachAgent(StopRecordingAgent):
        def __init__(self) -> None:
            super().__init__()
            self.reattach_calls: list[Path] = []

        def reattach_detached_run(self, path: Path):
            self.reattach_calls.append(path)
            return SimpleNamespace(
                run_id="run-1",
                sidecar_path=path,
                sidecar=sidecar,
                manifest=manifest,
            )

        def is_run_alive(self, run_id: str) -> bool:
            return run_id == "run-1"

    cancelled_groups: list[str] = []

    def capture_worker(coro, **_kwargs):
        coro.close()

    def cancel_group(_app: VllmLoaderApp, group: str) -> None:
        cancelled_groups.append(group)

    agent = ReattachAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)
        monkeypatch.setattr(app.workers, "cancel_group", cancel_group)

        app.reattach_detached_run(sidecar_path)
        app.action_stop()

        assert agent.reattach_calls == [sidecar_path]
        assert agent.stop_calls == [("run-1", 2, 2)]
        assert cancelled_groups == ["tail", "health"]
        assert app.reattached_sidecar_path is None
        assert app.reattached_run_id is None
        assert app.phase is Phase.STOPPED


@pytest.mark.asyncio
async def test_reattached_sidecar_liveness_uses_agent_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar_path = tmp_path / "detached.json"

    class AliveAgent(RecordingConfigAgent):
        def __init__(self) -> None:
            super().__init__()
            self.alive_calls: list[str] = []

        def is_run_alive(self, run_id: str) -> bool:
            self.alive_calls.append(run_id)
            return True

    def refuse_local_verify(_path: Path) -> bool:
        raise AssertionError("TUI should not verify an agent-reattached sidecar")

    agent = AliveAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(tui_app_module, "verify_sidecar_from_system", refuse_local_verify)
        app.reattached_sidecar_path = sidecar_path
        app.reattached_run_id = "run-1"

        assert app._sidecar_is_alive(sidecar_path) is True
        assert agent.alive_calls == ["run-1"]


@pytest.mark.asyncio
async def test_reattach_invalid_sidecar_shows_error_without_crashing(
    config_dir: Path, tmp_path: Path
) -> None:
    sidecar_path = tmp_path / "broken.json"
    sidecar_path.write_text("{not-json", encoding="utf-8")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()

        app.reattach_detached_run(sidecar_path)

        assert app.reattached_sidecar_path is None
        assert "Unable to reattach" in app.error_text
        assert "broken.json" in app.error_text


@pytest.mark.asyncio
async def test_reattach_health_worker_is_non_crashing_monitor(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "detached.log"
    log_path.write_text("INFO Uvicorn running on http://127.0.0.1:8000\n", encoding="utf-8")
    manifest_path = tmp_path / "detached.manifest.json"
    manifest = Manifest.from_active_log(log_path)
    sidecar_path = tmp_path / "detached.json"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="fake-child",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["fake-model"],
        exposure="local",
        manifest_path=str(manifest_path),
        config_snapshot={
            "name": "fake-child",
            "model": "fake/model",
            "server": {"host": "127.0.0.1", "port": 8000},
        },
    )
    worker_calls: list[dict[str, object]] = []

    def capture_worker(coro, **kwargs):
        worker_calls.append(kwargs)
        coro.close()

    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        app.reattach_detached_run(sidecar_path)

        health_worker = next(
            call for call in worker_calls if call["name"] == "reattach-health"
        )
        assert health_worker["group"] == "health"
        assert health_worker["exit_on_error"] is False


@pytest.mark.asyncio
async def test_reattach_hydrates_copyable_url_and_models_from_sidecar(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "detached.log"
    log_path.write_text("INFO Uvicorn running on http://0.0.0.0:8123\n", encoding="utf-8")
    manifest_path = tmp_path / "detached.manifest.json"
    manifest = Manifest.from_active_log(log_path)
    sidecar_path = tmp_path / "detached.json"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="fake-child",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="0.0.0.0",
        port=8123,
        served_model_names=["sidecar-model"],
        exposure="lan",
        manifest_path=str(manifest_path),
        config_snapshot={
            "name": "fake-child",
            "model": "fake/model",
            "server": {"host": "0.0.0.0", "port": 8123, "exposure": "lan"},
        },
    )

    def capture_worker(coro, **_kwargs):
        coro.close()

    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        app.reattach_detached_run(sidecar_path)

        assert app.ready_url == "http://127.0.0.1:8123"
        assert app.served_models == ["sidecar-model"]
        assert app._server_url_for_copy() == "http://127.0.0.1:8123"
        assert app.phase is Phase.SERVER_STARTING


@pytest.mark.asyncio
async def test_reattach_restores_registry_secrets_missing_from_sidecar_snapshot(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "secret-detached.yaml",
        """
        name: secret-detached
        model: registry/model
        server:
          host: 127.0.0.1
          port: 9001
          api_key: registry-api-key
        env:
          HF_TOKEN: registry-hf-token
        """,
    )
    log_path = tmp_path / "detached.log"
    log_path.write_text("INFO Uvicorn running on http://127.0.0.1:8000\n", encoding="utf-8")
    manifest_path = tmp_path / "detached.manifest.json"
    manifest = Manifest.from_active_log(log_path)
    sidecar_path = tmp_path / "detached.json"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="secret-detached",
        command_argv=["vllm", "serve", "snapshot/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["snapshot-model"],
        exposure="local",
        manifest_path=str(manifest_path),
        config_snapshot={
            "name": "secret-detached",
            "model": "snapshot/model",
            "server": {"host": "127.0.0.1", "port": 8000, "api_key": None},
            "env": {},
        },
    )

    def capture_worker(coro, **_kwargs):
        coro.close()

    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        app.reattach_detached_run(sidecar_path)

        assert app.current_config is not None
        assert app.current_config.model == "snapshot/model"
        assert app.current_config.server.port == 8000
        assert app.current_config.server.api_key == "registry-api-key"
        assert app.current_config.env["HF_TOKEN"] == "registry-hf-token"


@pytest.mark.asyncio
async def test_stop_after_detached_reattach_signals_verified_run(
    config_dir: Path, tmp_path: Path
) -> None:
    port = _free_port()
    runs_dir = tmp_path / "runs"
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "detached.yaml",
        f"""
        name: detached
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          mode: detached
          runs_dir: {runs_dir}
          health:
            interval_seconds: 0.05
        """,
    )
    cfg = load_registry(config_dir).by_name("detached")
    launch = start_detached(cfg, build_command(cfg), secrets=[])
    await _wait_for_log_text(launch.log_path, "Uvicorn running")

    app = VllmLoaderApp(configs_dir=config_dir)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.reattach_detached_run(launch.sidecar_path)
            await _wait_for_phase(app, Phase.READY)
            await pilot.press("s")
            await _wait_for_port_down(port)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_name", "helper_name", "expected_text"),
    [
        ("action_stop", "stop_sidecar_from_system", "Unable to stop"),
        ("action_kill", "signal_sidecar_from_system", "Unable to kill"),
    ],
)
async def test_detached_destructive_signal_mismatch_shows_error_without_detaching(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_name: str,
    helper_name: str,
    expected_text: str,
) -> None:
    sidecar_path = tmp_path / "stale.json"
    sidecar_path.write_text("{}", encoding="utf-8")

    def refuse_signal(*_args: object, **_kwargs: object) -> None:
        raise TrackedProcessMismatch(
            "tracked process is gone; refusing to signal a possibly-recycled PID"
        )

    monkeypatch.setattr(tui_app_module, helper_name, refuse_signal)
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.reattached_sidecar_path = sidecar_path
        app._set_phase(Phase.READY)

        getattr(app, action_name)()
        if action_name == "action_kill":
            await pilot.pause()
            assert app.screen.id == "confirm"
            assert app.reattached_sidecar_path == sidecar_path
            await pilot.press("enter")
            await pilot.pause()

        assert app.reattached_sidecar_path == sidecar_path
        assert app.phase is Phase.READY
        assert expected_text in app.error_text
        assert "possibly-recycled PID" in app.error_text


@pytest.mark.asyncio
async def test_detach_after_reattach_leaves_detached_server_running(
    config_dir: Path, tmp_path: Path
) -> None:
    port = _free_port()
    runs_dir = tmp_path / "runs"
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "detached.yaml",
        f"""
        name: detached
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          mode: detached
          runs_dir: {runs_dir}
          health:
            interval_seconds: 0.05
        """,
    )
    cfg = load_registry(config_dir).by_name("detached")
    launch = start_detached(cfg, build_command(cfg), secrets=[])
    await _wait_for_log_text(launch.log_path, "Uvicorn running")

    app = VllmLoaderApp(configs_dir=config_dir)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.reattach_detached_run(launch.sidecar_path)
            await _wait_for_phase(app, Phase.READY)

            commands = list(app.get_system_commands(app.screen))
            assert "Detach from detached run" in {command.title for command in commands}

            app.action_detach()

            assert app.reattached_sidecar_path is None
            await _wait_for_port_up(port)
            await pilot.press("s")
            await pilot.pause(0.2)

            assert app.phase is Phase.STOPPED
            await _wait_for_port_up(port)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_tui_load_honors_detached_launch_mode(config_dir: Path, tmp_path: Path) -> None:
    port = _free_port()
    runs_dir = tmp_path / "runs"
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "detached-load.yaml",
        f"""
        name: detached-load
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        vllm:
          version_profile: older-request-logging-on
        launch:
          mode: detached
          runs_dir: {runs_dir}
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_log(app, "Uvicorn running")
            await _wait_for_phase(app, Phase.READY)
            launched_process = app.current_process
            launched_sidecar_path = app.reattached_sidecar_path
            await pilot.press("s")
            await _wait_for_port_down(port)
            assert launched_process is None
            assert launched_sidecar_path is not None
            assert launched_sidecar_path.parent == runs_dir
            assert launched_sidecar_path.exists()
            sidecar = json.loads(launched_sidecar_path.read_text(encoding="utf-8"))
            assert sidecar["vllm_version_profile"] == "older-request-logging-on"
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_detached_tail_starts_from_loaded_log_position(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "run.log"
    log_path.write_text("INFO Initializing a V1 LLM engine\n", encoding="utf-8")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        loaded_position = app._load_scrubbed_log_file(log_path)
        log_path.write_text(
            "INFO Initializing a V1 LLM engine\n"
            "INFO Uvicorn running on http://127.0.0.1:8000\n",
            encoding="utf-8",
        )
        checks = 0

        def alive(_sidecar_path: Path) -> bool:
            nonlocal checks
            checks += 1
            return checks < 3

        monkeypatch.setattr(app, "_sidecar_is_alive", alive)
        await app._tail_detached_log(
            log_path, tmp_path / "sidecar.json", start_position=loaded_position
        )

        assert any("Uvicorn running" in line for line in app.log_lines)


@pytest.mark.asyncio
async def test_tui_detached_tail_consumes_agent_events(
    config_dir: Path, tmp_path: Path
) -> None:
    sidecar_path = tmp_path / "sidecar.json"
    log_path = tmp_path / "run.log"

    class TailAgent(RecordingConfigAgent):
        def __init__(self) -> None:
            super().__init__()
            self.tail_calls: list[tuple[str, int | None]] = []

        def is_run_alive(self, run_id: str) -> bool:
            return False

        async def tail_detached_run(
            self, run_id: str, *, start_position, emit_event, poll_interval=0.25
        ) -> None:
            self.tail_calls.append((run_id, start_position))
            emit_event(
                SimpleNamespace(
                    kind="log",
                    run_id=run_id,
                    payload={
                        "kind": "committed",
                        "text": "INFO Starting to load model",
                        "level": "INFO",
                    },
                )
            )
            emit_event(
                SimpleNamespace(
                    kind="phase",
                    run_id=run_id,
                    payload={"phase": Phase.LOADING_WEIGHTS.value},
                )
            )

    agent = TailAgent()
    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.reattached_sidecar_path = sidecar_path
        app.reattached_run_id = "run-1"

        await app._tail_detached_log(log_path, sidecar_path, start_position=123)
        await pilot.pause()

        assert agent.tail_calls == [("run-1", 123)]
        assert app.log_lines[-1] == "INFO Starting to load model"
        assert app.phase is Phase.LOADING_WEIGHTS


@pytest.mark.asyncio
async def test_detached_tail_classified_error_shows_named_banner(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "run.log"
    log_path.write_text("ERROR CUDA out of memory while profiling KV cache\n", encoding="utf-8")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        checks = 0

        def alive(_sidecar_path: Path) -> bool:
            nonlocal checks
            checks += 1
            return checks < 2

        monkeypatch.setattr(app, "_sidecar_is_alive", alive)
        await app._tail_detached_log(log_path, tmp_path / "sidecar.json", start_position=0)

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.OOM
        assert "OOM" in app.error_text
        assert "CUDA out of memory" in app.error_text
        assert "gpu_memory_utilization" in app.error_text


@pytest.mark.asyncio
async def test_detached_tail_reports_unexpected_disappearance_before_ready(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar_path = tmp_path / "sidecar.json"
    log_path = tmp_path / "run.log"
    log_path.write_text("INFO Starting to load model\n", encoding="utf-8")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.reattached_sidecar_path = sidecar_path
        checks = 0

        def alive(path: Path) -> bool:
            nonlocal checks
            assert path == sidecar_path
            checks += 1
            return checks < 2

        monkeypatch.setattr(app, "_sidecar_is_alive", alive)
        await app._tail_detached_log(log_path, sidecar_path, start_position=0)

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.CRASHED
        assert "CRASHED" in app.error_text
        assert "Starting to load model" in app.error_text


@pytest.mark.asyncio
async def test_loaded_detached_log_classified_error_shows_named_banner(
    config_dir: Path, tmp_path: Path
) -> None:
    log_path = tmp_path / "run.log"
    log_path.write_text("ERROR CUDA out of memory before reattach\n", encoding="utf-8")
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        loaded_position = app._load_scrubbed_log_file(log_path)

        assert loaded_position == log_path.stat().st_size
        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.OOM
        assert "OOM" in app.error_text
        assert "CUDA out of memory" in app.error_text
        assert "gpu_memory_utilization" in app.error_text


@pytest.mark.asyncio
async def test_fake_child_launch_streams_logs_and_stop_works(config_dir: Path) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "fake.yaml",
        f"""
        name: fake
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("l")
        await _wait_for_log(app, "Uvicorn running")
        await _wait_for_phase(app, Phase.READY)
        assert any("Initializing a V1 LLM engine" in line for line in app.log_lines)
        await pilot.press("s")
        await _wait_for_stopped(app)


@pytest.mark.asyncio
async def test_attached_tui_launch_uses_configured_runs_dir_for_durable_log(
    config_dir: Path, tmp_path: Path
) -> None:
    port = _free_port()
    runs_dir = tmp_path / "runs"
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "fake-runs.yaml",
        f"""
        name: fake-runs
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          runs_dir: {runs_dir}
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)
    durable_log = runs_dir / "fake-runs.run.log"

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_log(app, "Uvicorn running")
            await _wait_for_phase(app, Phase.READY)
            await pilot.press("s")
            await _wait_for_stopped(app)
    finally:
        await _cleanup_port(port)

    assert durable_log.exists()
    assert "Uvicorn running" in durable_log.read_text(encoding="utf-8")
    assert "checkpoint shards" not in durable_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_force_kill_running_attached_server_is_intentional_stop(
    config_dir: Path,
) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "fake.yaml",
        f"""
        name: fake
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_phase(app, Phase.READY)

            await pilot.press("K")
            await pilot.press("enter")
            await _wait_for_stopped(app)
            await _wait_for_phase(app, Phase.STOPPED)

            assert app.fsm.error_kind is None
            assert "CRASHED" not in app.error_text
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_ready_status_shows_server_url_and_served_models(config_dir: Path) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "fake.yaml",
        f"""
        name: fake
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_phase(app, Phase.READY)
            assert app.ready_url == f"http://127.0.0.1:{port}"
            assert app.served_models == ["fake-model"]
            assert f"READY http://127.0.0.1:{port} as fake-model" in app.status_text
            await pilot.press("s")
            await _wait_for_stopped(app)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_copy_server_url_uses_textual_clipboard(config_dir: Path) -> None:
    write_yaml(
        config_dir / "copy-url.yaml",
        """
        name: copy-url
        model: org/model
        server:
          host: 127.0.0.1
          port: 8124
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("copy-url")
        app._handle_health_event(HealthEvent(ready=True, detail="ready", models=["model"]))

        app.action_copy_server_url()

        assert app.last_copied_url == "http://127.0.0.1:8124"
        assert app.clipboard == "http://127.0.0.1:8124"


@pytest.mark.asyncio
async def test_restart_waits_for_attached_process_exit_before_loading(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "restart.yaml",
        """
        name: restart
        model: org/model
        """,
    )

    class SlowStoppingProcess:
        def __init__(self) -> None:
            self.proc = self
            self.pid = 12345
            self.stopped = False
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def stop(self, *args, **kwargs) -> None:
            self.stopped = True

    app = VllmLoaderApp(configs_dir=config_dir)
    fake_process = SlowStoppingProcess()
    load_calls: list[int | None] = []
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = app.registry.by_name("restart")
        app.current_process = fake_process
        monkeypatch.setattr(app, "action_load", lambda: load_calls.append(fake_process.poll()))
        monkeypatch.setattr(
            app,
            "notify",
            lambda message, *args, **kwargs: notifications.append(message),
        )

        app.action_restart()
        await pilot.pause()

        assert fake_process.stopped
        assert load_calls == []
        assert "A process is already running" not in notifications

        fake_process.running = False
        await _wait_for_condition(lambda: load_calls == [0], "restart did not load after exit")


@pytest.mark.asyncio
async def test_restart_attached_run_signals_target_client_by_run_id(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RestartRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            return run_id == "run-1"

        def stop_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct attached TUI restart stop")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "stop":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("restart stop should not subscribe")

    monkeypatch.setattr(tui_app_module, "InProcessTargetClient", FakeTargetClient)
    app = VllmLoaderApp(configs_dir=config_dir, agent=RestartRefusingAgent())
    load_calls: list[str | None] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"
        monkeypatch.setattr(app, "action_load", lambda: load_calls.append(app.current_run_id))

        app.action_restart()
        await _wait_for_condition(
            lambda: app._target_client.calls
            == [
                (
                    "stop",
                    {
                        "run_id": "run-1",
                        "interrupt_timeout": 2,
                        "terminate_timeout": 2,
                    },
                )
            ],
            "target client restart stop was not requested",
        )

        assert load_calls == []
        app.current_run_id = None
        await _wait_for_condition(
            lambda: load_calls == [None],
            "restart did not load after target run exit",
        )


@pytest.mark.asyncio
async def test_restart_waits_for_reattached_sidecar_exit_before_loading(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar_path = tmp_path / "detached.json"
    write_yaml(
        config_dir / "restart-detached.yaml",
        """
        name: restart-detached
        model: org/model
        """,
    )
    stopped_paths: list[Path] = []
    load_calls: list[Path | None] = []
    sidecar_alive = True

    def stop_sidecar(path: Path, **_kwargs: object) -> None:
        stopped_paths.append(path)

    def alive(path: Path) -> bool:
        assert path == sidecar_path
        return sidecar_alive

    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = load_registry(config_dir).by_name("restart-detached")
        app.reattached_sidecar_path = sidecar_path
        app._set_phase(Phase.READY)
        monkeypatch.setattr(tui_app_module, "stop_sidecar_from_system", stop_sidecar)
        monkeypatch.setattr(app, "_sidecar_is_alive", alive)
        monkeypatch.setattr(
            app,
            "action_load",
            lambda: load_calls.append(app.reattached_sidecar_path),
        )

        app.action_restart()
        await _wait_for_condition(
            lambda: stopped_paths == [sidecar_path],
            "sidecar stop was not requested",
        )

        assert app.reattached_sidecar_path == sidecar_path
        assert load_calls == []

        sidecar_alive = False
        await _wait_for_condition(
            lambda: load_calls == [None],
            "restart did not load after sidecar exit",
        )


@pytest.mark.asyncio
async def test_restart_after_agent_detached_reattach_signals_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar_path = tmp_path / "detached.json"
    write_yaml(
        config_dir / "restart-detached.yaml",
        """
        name: restart-detached
        model: org/model
        """,
    )
    agent = StopRecordingAgent()
    load_calls: list[Path | None] = []
    sidecar_alive = True

    def alive(path: Path) -> bool:
        assert path == sidecar_path
        return sidecar_alive

    app = VllmLoaderApp(configs_dir=config_dir, agent=agent)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = load_registry(config_dir).by_name("restart-detached")
        app.reattached_sidecar_path = sidecar_path
        app.reattached_run_id = "run-1"
        app._set_phase(Phase.READY)
        monkeypatch.setattr(app, "_sidecar_is_alive", alive)
        monkeypatch.setattr(
            app,
            "action_load",
            lambda: load_calls.append(app.reattached_sidecar_path),
        )

        app.action_restart()
        await _wait_for_condition(
            lambda: agent.stop_calls == [("run-1", 2, 2)],
            "agent stop was not requested",
        )

        assert app.reattached_sidecar_path == sidecar_path
        assert load_calls == []

        sidecar_alive = False
        await _wait_for_condition(
            lambda: load_calls == [None],
            "restart did not load after sidecar exit",
        )


@pytest.mark.asyncio
async def test_restart_stops_running_attached_server_and_starts_same_config(
    config_dir: Path,
) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "fake.yaml",
        f"""
        name: fake
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_phase(app, Phase.READY)
            first_start_count = sum(
                "Initializing a V1 LLM engine" in line for line in app.log_lines
            )
            assert app.current_run_id is not None

            await pilot.press("r")
            await _wait_for_log_count(
                app,
                "Initializing a V1 LLM engine",
                first_start_count + 1,
            )
            await _wait_for_phase(app, Phase.READY)

            assert app.current_config is not None
            assert app.current_config.name == "fake"
            assert app.current_process is None
            assert app.current_run_id is not None
            await pilot.press("s")
            await _wait_for_stopped(app)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_non_local_bind_warning_is_visible_in_tui(config_dir: Path) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "lan.yaml",
        f"""
        name: lan
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 0.0.0.0
          port: {port}
          exposure: lan
          api_key: sk-test
        launch:
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_phase(app, Phase.READY)
            assert app.ready_url == f"http://127.0.0.1:{port}"
            assert any("reachable beyond localhost" in line for line in app.warning_lines)
            assert "`--api-key` does not protect all endpoints" in app.error_text
            await pilot.press("s")
            await _wait_for_port_down(port)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_quit_while_attached_running_prompts_stop_or_cancel(config_dir: Path) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "fake.yaml",
        f"""
        name: fake
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          health:
            interval_seconds: 0.05
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("l")
        await _wait_for_phase(app, Phase.READY)
        await pilot.press("q")
        await pilot.pause()
        assert app.screen.id == "confirm"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.id != "confirm"
        assert app.current_run_id is not None
        assert app._attached_run_is_alive()
        await pilot.press("q")
        await pilot.press("enter")
        await _wait_for_condition(lambda: app.is_running is False, "quit did not exit")
        assert not app._attached_run_is_alive()
        assert app.is_running is False


@pytest.mark.asyncio
async def test_quit_confirm_stop_waits_for_attached_process_exit(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowStoppingProcess:
        def __init__(self) -> None:
            self.proc = self
            self.pid = 12345
            self.stopped = False
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def stop(self, *args, **kwargs) -> None:
            self.stopped = True

    app = VllmLoaderApp(configs_dir=config_dir)
    fake_process = SlowStoppingProcess()
    exit_calls: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_process = fake_process
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))

        app.confirm_stop_running()
        await pilot.pause()

        assert fake_process.stopped
        assert exit_calls == []

        fake_process.running = False
        await _wait_for_condition(lambda: exit_calls == [True], "quit did not exit after stop")


@pytest.mark.asyncio
async def test_quit_confirm_stop_attached_run_signals_target_client_by_run_id(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class QuitStopRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            return run_id == "run-1"

        def stop_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct attached TUI quit stop")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "stop":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("quit stop should not subscribe")

    monkeypatch.setattr(tui_app_module, "InProcessTargetClient", FakeTargetClient)
    app = VllmLoaderApp(configs_dir=config_dir, agent=QuitStopRefusingAgent())
    exit_calls: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))

        app.confirm_stop_running()
        await _wait_for_condition(
            lambda: app._target_client.calls
            == [
                (
                    "stop",
                    {
                        "run_id": "run-1",
                        "interrupt_timeout": 2,
                        "terminate_timeout": 2,
                    },
                )
            ],
            "target client quit stop was not requested",
        )

        assert exit_calls == []
        app.current_run_id = None
        await _wait_for_condition(lambda: exit_calls == [True], "quit did not exit after run")


@pytest.mark.asyncio
async def test_log_filter_and_search_are_functional(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app._write_log("INFO ready")
        app._write_log("ERROR bad thing", "ERROR")
        app.apply_log_filter("ERROR")
        await pilot.pause()
        assert app.visible_log_lines == ["ERROR bad thing"]
        app.apply_log_search("bad")
        assert app.search_matches == ["ERROR bad thing"]
        app.apply_log_search("ready")
        assert app.search_matches == []
        app.apply_log_filter("")
        assert "INFO ready" in app.visible_log_lines


@pytest.mark.asyncio
async def test_log_filter_accepts_warn_alias_for_warning_level(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app._write_log("slow load", "WARNING")
        app._write_log("ready", "INFO")

        app.apply_log_filter("WARN")
        await pilot.pause()

        assert app.visible_log_lines == ["slow load"]


@pytest.mark.asyncio
async def test_search_key_prompts_and_applies_submitted_text(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app._write_log("INFO ready")
        app._write_log("ERROR bad thing", "ERROR")
        await pilot.press("/", "b", "a", "d", "enter")
        await pilot.pause()

        assert app.search_text == "bad"
        assert app.search_matches == ["ERROR bad thing"]


@pytest.mark.asyncio
async def test_filter_key_prompts_and_applies_submitted_text(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app._write_log("INFO ready")
        app._write_log("ERROR bad thing", "ERROR")
        await pilot.press("f", "E", "R", "R", "O", "R", "enter")
        await pilot.pause()

        assert app.filter_text == "ERROR"
        assert app.visible_log_lines == ["ERROR bad thing"]


@pytest.mark.asyncio
async def test_log_search_highlights_matching_text_in_view(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app._write_log("ERROR bad thing", "ERROR")
        await pilot.pause()

        app.apply_log_search("bad")
        await pilot.pause()

        log = app.query_one("#log", RichLog)
        highlight_segments = [
            segment
            for strip in log.lines
            if "ERROR bad thing" in strip.text
            for segment in strip._segments
            if segment.text == "bad"
        ]

        assert highlight_segments
        assert any(
            segment.style is not None and segment.style.bgcolor is not None
            for segment in highlight_segments
        )


@pytest.mark.asyncio
async def test_bursty_log_output_batches_richlog_writes(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one("#log", RichLog)
        baseline_lines = len(log.lines)

        for index in range(25):
            app._write_log(f"INFO burst line {index}", "INFO")

        assert app.log_lines[-1] == "INFO burst line 24"
        assert app.visible_log_lines[-1] == "INFO burst line 24"
        assert len(log.lines) == baseline_lines

        await _wait_for_condition(
            lambda: any("INFO burst line 24" in strip.text for strip in log.lines),
            "batched log line was not flushed to RichLog",
        )


@pytest.mark.asyncio
async def test_transient_progress_updates_progress_bar_without_committing_log(
    config_dir: Path,
) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app.handle_log_record(
            LogRecord("transient", "Loading checkpoint shards: 45%|####      |", None)
        )
        await pilot.pause()

        progress = app.query_one("#progress", ProgressBar)

        assert progress.total == 100
        assert progress.progress == 45
        assert "Loading checkpoint shards" in app.progress_text
        assert not any("Loading checkpoint shards" in line for line in app.log_lines)


@pytest.mark.asyncio
async def test_responsive_layout_keeps_log_visible_on_narrow_terminals(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "narrow.yaml",
        """
        name: narrow
        model: org/narrow
        engine:
          tensor_parallel_size: 2
          kv_cache_dtype: fp8
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        sidebar = app.query_one("#sidebar")
        sidebar_overlay = app.query_one("#sidebar-overlay", Static)
        gpu_panel = app.query_one("#gpu")
        log = app.query_one("#log", RichLog)

        assert app.responsive_mode == "wide"
        assert sidebar.display is True
        assert sidebar_overlay.display is False
        assert gpu_panel.display is True
        assert log.display is True

        await pilot.resize_terminal(99, 40)
        await pilot.pause()

        assert app.responsive_mode == "narrow"
        assert sidebar.display is False
        assert sidebar_overlay.display is True
        assert gpu_panel.display is True
        assert log.display is True
        assert isinstance(sidebar_overlay.content, Text)
        assert "Sidebar overlay" in sidebar_overlay.content.plain
        assert "narrow" in sidebar_overlay.content.plain
        assert "IDLE" in sidebar_overlay.content.plain

        await pilot.resize_terminal(59, 40)
        await pilot.pause()

        assert app.responsive_mode == "compact"
        assert sidebar.display is False
        assert sidebar_overlay.display is True
        assert gpu_panel.display is False
        assert log.display is True

        await pilot.resize_terminal(120, 40)
        await pilot.pause()

        assert app.responsive_mode == "wide"
        assert sidebar.display is True
        assert sidebar_overlay.display is False
        assert gpu_panel.display is True
        assert log.display is True


@pytest.mark.asyncio
async def test_log_buffers_are_bounded_for_bursty_output(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir, max_log_lines=3)

    async with app.run_test() as pilot:
        for index in range(5):
            app._write_log(f"INFO line-{index}", "INFO")
        await pilot.pause()

        assert app.query_one("#log", RichLog).max_lines == 3
        assert app.log_lines == ["INFO line-2", "INFO line-3", "INFO line-4"]
        assert app.log_records == [
            ("INFO line-2", "INFO"),
            ("INFO line-3", "INFO"),
            ("INFO line-4", "INFO"),
        ]
        assert app.visible_log_lines == ["INFO line-2", "INFO line-3", "INFO line-4"]

        app.apply_log_search("line-0")
        assert app.search_matches == []
        app.apply_log_search("line-4")
        assert app.search_matches == ["INFO line-4"]


@pytest.mark.asyncio
async def test_debug_log_records_structured_app_events(
    config_dir: Path, tmp_path: Path
) -> None:
    debug_log_path = tmp_path / "debug.jsonl"
    app = VllmLoaderApp(configs_dir=config_dir, debug_log_path=debug_log_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._write_log("INFO observed", "INFO")
        app._set_phase(Phase.STARTING)

    records = [
        json.loads(line)
        for line in debug_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(record["event"] == "app.mounted" for record in records)
    assert any(
        record["event"] == "log.committed"
        and record["payload"]["text"] == "INFO observed"
        and record["payload"]["level"] == "INFO"
        for record in records
    )
    assert any(
        record["event"] == "phase.changed"
        and record["payload"]["phase"] == "STARTING"
        for record in records
    )


@pytest.mark.asyncio
async def test_pause_toggles_richlog_autoscroll(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        log = app.query_one("#log", RichLog)
        assert log.auto_scroll is True
        await pilot.press("p")
        await pilot.pause()
        assert app.paused is True
        assert log.auto_scroll is False
        await pilot.press("p")
        await pilot.pause()
        assert app.paused is False
        assert log.auto_scroll is True


async def _wait_for_log(app: VllmLoaderApp, text: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if any(text in line for line in app.log_lines):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"log line {text!r} was not emitted")


async def _wait_for_command(app: VllmLoaderApp, title: str):
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        for command in app.get_system_commands(app.screen):
            if command.title == title:
                return command
        await asyncio.sleep(0.05)
    raise AssertionError(f"command {title!r} was not available")


async def _wait_for_gpu_text(app: VllmLoaderApp, text: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if text in app.gpu_panel_text:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"GPU panel text {text!r} was not shown")


async def _wait_for_gpu_calls(calls: list[int], count: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if len(calls) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"GPU sampler was called {len(calls)} times, expected {count}")


async def _wait_for_stopped(app: VllmLoaderApp) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if app.current_process and app.current_process.proc.poll() is not None:
            return
        if (
            app.current_process is None
            and app.current_run_id is None
            and app.phase is Phase.STOPPED
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("fake child did not stop")


async def _wait_for_condition(condition, message: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


async def _wait_for_log_count(app: VllmLoaderApp, text: str, count: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if sum(text in line for line in app.log_lines) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"log line {text!r} did not reach count {count}")


async def _wait_for_phase(app: VllmLoaderApp, phase: Phase) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if app.phase is phase:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"phase {phase.value} was not reached")


async def _wait_for_log_text(path: Path, text: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if path.exists() and text in path.read_text(encoding="utf-8"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{text!r} was not written to {path}")


def _static_text(app: VllmLoaderApp, selector: str) -> str:
    return str(app.query_one(selector, Static).content)


def _text_uses_style(text: Text, style_fragment: str) -> bool:
    if text.style and style_fragment in str(text.style):
        return True
    return any(style_fragment in str(span.style) for span in text.spans)


async def _cleanup_port(port: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        "lsof",
        "-ti",
        f"tcp:{port}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    for pid_text in stdout.decode().splitlines():
        if pid_text.strip():
            try:
                await asyncio.create_subprocess_exec("kill", "-TERM", pid_text.strip())
            except Exception:
                pass


async def _wait_for_port_down(port: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            return
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)
    raise AssertionError(f"port {port} stayed open")


async def _wait_for_port_up(port: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise AssertionError(f"port {port} did not open")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
