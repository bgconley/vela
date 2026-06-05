from __future__ import annotations

import asyncio
import inspect
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_yaml
from rich.text import Text
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, ProgressBar, RichLog, Select, Static
from textual.worker import WorkerState

from vllm_loader.agent.local import LocalAgent, TargetCallError
from vllm_loader.config.loader import load_registry
from vllm_loader.config.targets import TargetConfig, TransportKind
from vllm_loader.engine.command_builder import build_command
from vllm_loader.engine.log_sink import LogRecord
from vllm_loader.engine.phases import ErrorKind, Phase
from vllm_loader.engine.process_manager import start_detached
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
from vllm_loader.transport.inprocess import InProcessTargetClient
from vllm_loader.tui import app as tui_app_module
from vllm_loader.tui.app import VllmLoaderApp
from vllm_loader.tui.screens import config_picker as config_picker_module
from vllm_loader.tui.screens.confirm import ConfirmScreen


def test_wire_job_events_map_to_existing_tui_messages() -> None:
    progress = tui_app_module._message_from_wire_event(
        {
            "event": "job_progress",
            "job_id": "job-1",
            "kind": "committed",
            "text": "Downloading model",
            "level": "INFO",
            "seq": 1,
            "ts": "2026-06-03T00:00:00Z",
            "mono": 1.0,
        }
    )
    done = tui_app_module._message_from_wire_event(
        {
            "event": "job_done",
            "job_id": "job-1",
            "ok": False,
            "error_kind": "config-invalid",
            "detail": "download failed",
            "seq": 2,
            "ts": "2026-06-03T00:00:01Z",
            "mono": 2.0,
        }
    )

    assert isinstance(progress, LogLineCommitted)
    assert progress.text == "Downloading model"
    assert progress.feed_phase is False
    assert isinstance(done, EngineError)
    assert done.kind is ErrorKind.CONFIG_INVALID
    assert done.detail == "download failed"


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
        if method == "preflight":
            self.calls.append((method, params))
            return {"ok": True, "failures": []}
        raise AssertionError(f"unexpected method: {method}")

    def discover_detached_runs(self, runs_dirs):
        return []


class RecordingLaunchPrepareAgent(RecordingConfigAgent):
    def handle(self, method: str, params: dict[str, str] | None = None):
        if method == "preflight":
            self.calls.append((method, params))
            return {
                "ok": False,
                "failures": [
                    {"kind": "MODEL_NOT_FOUND", "detail": "agent-side missing model"}
                ],
            }
        if method == "prepare_launch":
            raise AssertionError("prepare_launch should not run after preflight failure")
        return super().handle(method, params)


_TARGET_CONFIG_METHODS = {"list_configs", "preview", "preflight", "prepare_launch"}


def _delegate_config_target_call(agent, method: str, params):
    return agent.handle(method, params)


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
async def test_tui_loads_registry_and_preview_through_target_client(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class HandleRefusingAgent:
        def handle(self, method: str, _params=None):
            raise AssertionError(f"direct TUI handle call: {method}")

    client_instances: list[object] = []

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, str]]] = []
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
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
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/alpha", "warnings": []}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("config load should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(HandleRefusingAgent()),
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.current_config is not None
        assert app.current_config.name == "alpha"
        assert "alpha" in app.config_summary
        assert "vllm serve org/alpha" in app.selected_config_preview
        assert client_instances[0].calls[:2] == [
            ("list_configs", {"configs_dir": str(config_dir)}),
            ("preview", {"name": "alpha", "configs_dir": str(config_dir)}),
        ]


@pytest.mark.asyncio
async def test_tui_accepts_injected_target_client_without_local_agent(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InjectedTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/target/configs/remote.yaml",
                            "name": "remote",
                            "model": "org/remote",
                            "target": "remote",
                            "warnings": [],
                            "config": {
                                "name": "remote",
                                "target": "remote",
                                "model": "org/remote",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "cwd=/target\nvllm serve org/remote", "warnings": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("injected target setup should not subscribe")

    target_client = InjectedTargetClient()
    app = VllmLoaderApp(configs_dir=config_dir, target_client=target_client)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.current_config is not None
        assert app.current_config.name == "remote"
        assert app.current_config.target == "remote"
        assert target_client.calls[:2] == [
            ("list_configs", {"configs_dir": str(config_dir)}),
            ("preview", {"name": "remote", "configs_dir": str(config_dir)}),
        ]


@pytest.mark.asyncio
async def test_header_target_segment_tracks_connection_state(
    config_dir: Path,
) -> None:
    class QuietTargetClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("header target segment should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=QuietTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.pause()

        expected_dots = {
            "connected": "●",
            "connecting": "◐",
            "reconnecting": "◐",
            "disconnected": "○",
            "unreachable": "✕",
            "version-mismatch": "▲",
        }
        for state, dot in expected_dots.items():
            app.target_connection_state = state
            app._refresh_chrome()
            await pilot.pause()

            segment = _static_text(app, "#target-segment")
            assert "⊕ blackbird" in segment
            assert dot in segment

        app.target_connection_state = "connected"
        app._refresh_chrome()

        await pilot.resize_terminal(99, 45)
        await pilot.pause()
        assert _static_text(app, "#target-segment") == "⊕bbrd●"

        await pilot.resize_terminal(59, 45)
        await pilot.pause()
        assert _static_text(app, "#target-segment") == "⊕●"

        await pilot.resize_terminal(144, 45)
        await pilot.pause()
        assert _static_text(app, "#target-segment") == "⊕ blackbird ●"


@pytest.mark.asyncio
async def test_target_manager_screen_opens_from_binding(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        workdir=Path("/tank/repos/lab-tui"),
        venv=Path("/tank/venvs/lab-tui"),
    )

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [TargetConfig(name="local"), blackbird]

        def by_name(self, name: str) -> TargetConfig:
            if name == "blackbird":
                return blackbird
            if name == "local":
                return TargetConfig(name="local")
            raise KeyError(name)

    class QuietTargetClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "agent_version": "0.9.0-agent",
                "controller_version": "0.9.0-controller",
                "agent_protocol_version": 1,
                "protocol_version": 1,
                "target": "blackbird",
                "daemon_start_ts": "2026-06-03T00:00:00Z",
                "capabilities": [
                    "list_configs",
                    "preview",
                    "prepare_launch",
                    "gpu",
                    "health",
                ],
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {
                    "runs": [
                        {"run_id": "run-alpha", "config_name": "alpha"},
                        {"run_id": "run-beta", "config_name": "beta"},
                    ]
                }
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target manager should not subscribe")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=QuietTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _wait_for_condition(
            lambda: len(app.detached_run_summaries) == 2,
            "detached runs were not discovered",
        )
        app.gpu_panel_text = "0 A100 1024/81920MB 25%"
        await pilot.press("t")
        await pilot.pause()

        assert app.screen.id == "target-manager"
        assert isinstance(app.screen, ModalScreen)
        target_list = str(app.screen.query_one("#target-manager-list", Static).content)
        detail = str(app.screen.query_one("#target-manager-detail", Static).content)
        assert "> ● blackbird  ssh  bgconley@10.25.0.51" in target_list
        assert "Target Manager" in target_list
        assert "workdir: /tank/repos/lab-tui" in detail
        assert "venv: /tank/venvs/lab-tui" in detail
        assert "connection: connected" in detail
        assert "agent: 0.9.0-agent" in detail
        assert "controller: 0.9.0-controller" in detail
        assert "protocol: 1" in detail
        assert "capabilities: gpu, health, list_configs, prepare_launch, preview" in detail
        assert "active_runs: 2 (alpha, beta)" in detail
        assert "gpu: 0 A100 1024/81920MB 25%" in detail
        assert "last_seen:" in detail


@pytest.mark.asyncio
async def test_target_manager_selection_switches_target_and_refreshes_configs(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
    )

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [TargetConfig(name="local"), blackbird]

        def by_name(self, name: str) -> TargetConfig:
            if name == "local":
                return TargetConfig(name="local")
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    class FactoryTargetClient:
        def __init__(self, target: TargetConfig) -> None:
            self.target = target
            self.connected = False
            self.disconnect_calls = 0

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False
            self.disconnect_calls += 1

        async def call(self, method: str, params):
            if method == "list_configs":
                name = (
                    "remote-selected"
                    if self.target.name == "blackbird"
                    else "local-selected"
                )
                return {
                    "valid": [
                        {
                            "path": f"/{self.target.name}/configs/{name}.yaml",
                            "name": name,
                            "model": f"org/{name}",
                            "target": self.target.name,
                            "warnings": [],
                            "config": {
                                "name": name,
                                "target": self.target.name,
                                "model": f"org/{name}",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": f"cwd=/{self.target.name}\nvllm serve {params['name']}",
                    "warnings": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target selection should not subscribe")

    target_clients: list[FactoryTargetClient] = []
    requested_targets: list[str] = []

    def fake_target_client_for_config(target, **_kwargs):
        requested_targets.append(target.name)
        client = FactoryTargetClient(target)
        target_clients.append(client)
        return client

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        fake_target_client_for_config,
    )

    app = VllmLoaderApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        assert app.current_config is not None
        assert app.current_config.name == "local-selected"

        await pilot.press("t")
        await pilot.press("down")
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: app.target_name == "blackbird"
            and app.current_config is not None
            and app.current_config.name == "remote-selected",
            "target manager did not switch to blackbird",
        )
        await pilot.pause()

        assert requested_targets == ["local", "blackbird"]
        assert target_clients[0].disconnect_calls == 1
        assert _static_text(app, "#target-segment") == "⊕ blackbird ●"
        assert "remote-selected" in _static_text(app, "#active-model")


@pytest.mark.asyncio
async def test_target_manager_add_persists_new_target(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = [TargetConfig(name="local")]
    saved_targets: list[TargetConfig] = []

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return list(targets)

        def by_name(self, name: str) -> TargetConfig:
            for target in targets:
                if target.name == name:
                    return target
            raise KeyError(name)

    class QuietTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target add should not subscribe")

    def fake_upsert_target_file(target: TargetConfig) -> Path:
        saved_targets.append(target)
        targets[:] = [item for item in targets if item.name != target.name]
        targets.append(target)
        return Path("/agent/targets.yaml")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        tui_app_module,
        "upsert_target_file",
        fake_upsert_target_file,
        raising=False,
    )
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=QuietTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("t")
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "target-edit",
            "target add form did not open",
        )
        app.screen.query_one("#target-edit-input", Input).value = (
            "name=blackbird transport=ssh host=bgconley@10.25.0.51 "
            "workdir=/tank/repos/lab-tui venv=/tank/venvs/lab-tui"
        )
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: app.screen.id == "target-manager",
            "target manager did not reopen after add",
        )

        assert [target.name for target in saved_targets] == ["blackbird"]
        saved = saved_targets[0]
        assert saved.transport is TransportKind.SSH
        assert saved.host == "bgconley@10.25.0.51"
        assert saved.workdir == Path("/tank/repos/lab-tui")
        assert saved.venv == Path("/tank/venvs/lab-tui")
        target_list = str(app.screen.query_one("#target-manager-list", Static).content)
        assert "blackbird  ssh  bgconley@10.25.0.51" in target_list


@pytest.mark.asyncio
async def test_target_manager_edit_persists_selected_target(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
    )
    targets = [TargetConfig(name="local"), blackbird]
    saved_targets: list[TargetConfig] = []

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return list(targets)

        def by_name(self, name: str) -> TargetConfig:
            for target in targets:
                if target.name == name:
                    return target
            raise KeyError(name)

    class QuietTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target edit should not subscribe")

    def fake_upsert_target_file(target: TargetConfig) -> Path:
        saved_targets.append(target)
        targets[:] = [item for item in targets if item.name != target.name]
        targets.append(target)
        return Path("/agent/targets.yaml")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        tui_app_module,
        "upsert_target_file",
        fake_upsert_target_file,
        raising=False,
    )
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=QuietTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("t")
        await pilot.press("down")
        await pilot.press("e")
        await _wait_for_condition(
            lambda: app.screen.id == "target-edit",
            "target edit form did not open",
        )
        form = app.screen.query_one("#target-edit-input", Input)
        assert "name=blackbird" in form.value
        form.value = "name=blackbird transport=ssh host=bgconley@10.25.0.52"
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: app.screen.id == "target-manager",
            "target manager did not reopen after edit",
        )

        assert [target.host for target in saved_targets] == ["bgconley@10.25.0.52"]
        target_list = str(app.screen.query_one("#target-manager-list", Static).content)
        assert "blackbird  ssh  bgconley@10.25.0.52" in target_list


@pytest.mark.asyncio
async def test_target_manager_remove_confirms_and_updates_registry(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
    )
    targets = [TargetConfig(name="local"), blackbird]
    removed_targets: list[str] = []

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return list(targets)

        def by_name(self, name: str) -> TargetConfig:
            for target in targets:
                if target.name == name:
                    return target
            raise KeyError(name)

    class QuietTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target remove should not subscribe")

    def fake_remove_target_file(name: str) -> Path:
        removed_targets.append(name)
        targets[:] = [target for target in targets if target.name != name]
        return Path("/agent/targets.yaml")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        tui_app_module,
        "remove_target_file",
        fake_remove_target_file,
        raising=False,
    )
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=QuietTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("t")
        await pilot.press("down")
        await pilot.press("x")
        await _wait_for_condition(
            lambda: app.screen.id == "confirm",
            "target remove confirm did not open",
        )
        confirm_text = str(app.screen.query_one("#confirm-message", Static).content)
        assert "Remove target blackbird?" in confirm_text

        await pilot.press("enter")

        await _wait_for_condition(
            lambda: removed_targets == ["blackbird"],
            "target remove was not persisted",
        )
        await pilot.press("t")
        await pilot.pause()

        target_list = str(app.screen.query_one("#target-manager-list", Static).content)
        assert "blackbird" not in target_list


@pytest.mark.asyncio
async def test_tui_surfaces_target_version_mismatch_on_mount(
    config_dir: Path,
) -> None:
    class VersionMismatchTargetClient:
        connected = False

        async def connect(self):
            raise TargetCallError(
                "version-mismatch",
                "controller protocol version 2 is newer than agent protocol 1",
                {"required": 2, "actual": 1},
            )

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            raise AssertionError(f"unexpected target client call: {method}")

        async def ping(self):
            raise AssertionError("version mismatch should not ping")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("version mismatch should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=VersionMismatchTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.target_connection_state == "version-mismatch"
        assert "controller protocol version 2" in app.target_connection_detail
        assert "AGENT_VERSION_MISMATCH" in app.error_text
        assert "controller protocol version 2" in app.error_text
        assert "(R) Reconnect" in app.error_text
        assert "(t) Switch target" in app.error_text
        assert app.registry.valid == []
        assert app.registry.invalid == []


@pytest.mark.asyncio
async def test_action_load_blocks_when_target_unreachable(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    app = VllmLoaderApp(configs_dir=config_dir)
    worker_calls: list[str] = []

    def capture_worker(coro, **kwargs):
        worker_calls.append(str(kwargs.get("name", "")))
        coro.close()

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)
        app.target_connection_state = "unreachable"
        app.target_connection_detail = "ssh connection refused"
        app.action_load()

        assert worker_calls == []
        assert "target unreachable" in app.error_text
        assert "ssh connection refused" in app.error_text
        assert "(R) Reconnect" in app.error_text
        assert "(t) Switch target" in app.error_text


@pytest.mark.asyncio
async def test_run_control_actions_block_when_target_disconnected(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_ping_interval_seconds=None,
    )
    worker_calls: list[str] = []

    def capture_worker(coro, **kwargs):
        worker_calls.append(str(kwargs.get("name", "")))
        coro.close()

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)
        app.target_connection_state = "disconnected"
        app.target_connection_detail = "ping timeout"
        app.current_run_id = "run-1"

        app.action_stop()
        app.action_kill()
        app.action_restart()

        assert worker_calls == []
        assert app.screen.id != "confirm"
        assert "target disconnected" in app.error_text
        assert "ping timeout" in app.error_text
        assert "(R) Reconnect" in app.error_text
        assert "(t) Switch target" in app.error_text


@pytest.mark.asyncio
async def test_resource_manager_actions_block_when_target_lacks_capability(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LimitedCapabilityTargetClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "agent_version": "0.9.0",
                "agent_protocol_version": 1,
                "protocol_version": 1,
                "capabilities": [
                    "list_configs",
                    "preview",
                    "prepare_launch",
                    "launch",
                ],
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/alpha.yaml",
                            "name": "alpha",
                            "model": "org/alpha",
                            "warnings": [],
                            "config": {
                                "name": "alpha",
                                "model": "org/alpha",
                            },
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "vllm serve org/alpha", "warnings": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("capability gate should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="limited",
        target_client=LimitedCapabilityTargetClient(),
        target_ping_interval_seconds=None,
    )
    worker_calls: list[str] = []

    def capture_worker(coro, **kwargs):
        worker_calls.append(str(kwargs.get("name", "")))
        coro.close()

    async with app.run_test():
        await _wait_for_target_connection_state(app, "connected")
        assert app.current_config is not None
        monkeypatch.setattr(app, "run_worker", capture_worker)

        app.action_builds()
        app.action_models()
        app.action_flags()

        assert worker_calls == []
        assert "Feature not available on limited" in app.error_text
        assert "update_config_flags" in app.error_text


@pytest.mark.asyncio
async def test_tui_keepalive_timeout_marks_target_disconnected(
    config_dir: Path,
) -> None:
    class HangingPingTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.disconnect_calls = 0

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False
            self.disconnect_calls += 1

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        async def ping(self):
            await asyncio.sleep(60)

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("keepalive should not subscribe")

    target_client = HangingPingTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=target_client,
        target_ping_interval_seconds=0.01,
        target_ping_timeout_seconds=0.01,
    )

    async with app.run_test():
        await _wait_for_condition(
            lambda: target_client.disconnect_calls >= 1
            and app.target_connection_state == "disconnected"
            and "ping timeout" in app.target_connection_detail,
            "target was not marked disconnected after ping timeout",
        )

        assert target_client.disconnect_calls >= 1
        assert "ping timeout" in app.target_connection_detail


def test_target_keepalive_uses_exponential_reconnect_backoff(
    config_dir: Path,
) -> None:
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_ping_interval_seconds=30,
    )

    app.target_connection_state = "connected"
    assert app._target_keepalive_delay_seconds() == 30

    app.target_connection_state = "unreachable"
    assert app._target_keepalive_delay_seconds() == 0.1

    app._update_target_reconnect_backoff()
    assert app._target_keepalive_delay_seconds() == 0.2

    app._target_reconnect_backoff_seconds = 8.0
    app._update_target_reconnect_backoff()
    assert app._target_keepalive_delay_seconds() == 10.0

    app.target_connection_state = "connected"
    app._update_target_reconnect_backoff()
    assert app._target_keepalive_delay_seconds() == 30
    assert app._target_reconnect_backoff_seconds == 0.1


@pytest.mark.asyncio
async def test_tui_keepalive_timeout_reconnects_to_target(
    config_dir: Path,
) -> None:
    reconnect_started = asyncio.Event()
    allow_reconnect = asyncio.Event()

    class ReconnectingTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.connect_calls = 0
            self.disconnect_calls = 0
            self.ping_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            if self.connect_calls > 1:
                reconnect_started.set()
                await allow_reconnect.wait()
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False
            self.disconnect_calls += 1

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        async def ping(self):
            self.ping_calls += 1
            if self.ping_calls == 1:
                await asyncio.sleep(60)
            return {
                "pong": True,
                "target": "local",
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("keepalive reconnect should not subscribe")

    target_client = ReconnectingTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=target_client,
        target_ping_interval_seconds=0.01,
        target_ping_timeout_seconds=0.01,
    )

    async with app.run_test() as pilot:
        await asyncio.wait_for(reconnect_started.wait(), timeout=5)
        await _wait_for_target_connection_state(app, "reconnecting")
        allow_reconnect.set()
        await _wait_for_target_connection_state(app, "connected")
        await pilot.pause()

        assert target_client.connect_calls >= 2
        assert target_client.disconnect_calls >= 1
        assert app.target_connection_detail == ""


@pytest.mark.asyncio
async def test_tui_reconnect_detects_agent_restart_and_rediscovers_runs(
    config_dir: Path,
) -> None:
    reconnect_started = asyncio.Event()
    allow_reconnect = asyncio.Event()

    class RestartingTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.connect_calls = 0
            self.disconnect_calls = 0
            self.ping_calls = 0
            self.calls: list[tuple[str, dict[str, object] | None]] = []

        async def connect(self):
            self.connect_calls += 1
            if self.connect_calls > 1:
                reconnect_started.set()
                await allow_reconnect.wait()
            self.connected = True
            return {
                "agent_version": "test",
                "protocol_version": 1,
                "target": "local",
                "daemon_start_ts": (
                    "2026-06-03T00:00:00Z"
                    if self.connect_calls == 1
                    else "2026-06-03T00:01:00Z"
                ),
            }

        async def disconnect(self) -> None:
            self.connected = False
            self.disconnect_calls += 1

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        async def ping(self):
            self.ping_calls += 1
            if self.ping_calls == 1:
                await asyncio.sleep(60)
            return {
                "pong": True,
                "target": "local",
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("keepalive reconnect should not subscribe")

    target_client = RestartingTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=target_client,
        target_ping_interval_seconds=0.01,
        target_ping_timeout_seconds=0.01,
    )

    async with app.run_test() as pilot:
        await asyncio.wait_for(reconnect_started.wait(), timeout=5)
        initial_discover_calls = [
            call for call in target_client.calls if call[0] == "discover_runs"
        ]
        allow_reconnect.set()
        await _wait_for_target_connection_state(app, "connected")
        await _wait_for_condition(
            lambda: app.target_agent_restarted is True,
            "agent restart was not detected",
        )
        await _wait_for_condition(
            lambda: len(
                [call for call in target_client.calls if call[0] == "discover_runs"]
            )
            > len(initial_discover_calls),
            "detached runs were not rediscovered after agent restart",
        )
        await pilot.pause()

        assert target_client.connect_calls >= 2
        assert target_client.disconnect_calls >= 1


@pytest.mark.asyncio
async def test_tui_default_local_target_uses_target_client_factory(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FactoryTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/factory/configs/local.yaml",
                            "name": "factory-local",
                            "model": "org/factory",
                            "target": None,
                            "warnings": [],
                            "config": {"name": "factory-local", "model": "org/factory"},
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "cwd=/factory\nvllm serve org/factory", "warnings": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("default target setup should not subscribe")

    target_clients: list[FactoryTargetClient] = []
    requested_targets: list[str] = []

    def fake_target_client_for_config(target, **_kwargs):
        requested_targets.append(target.name)
        client = FactoryTargetClient()
        target_clients.append(client)
        return client

    monkeypatch.setattr(tui_app_module, "target_client_for_config", fake_target_client_for_config)
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert requested_targets == ["local"]
        assert app.current_config is not None
        assert app.current_config.name == "factory-local"
        assert target_clients[0].calls[:2] == [
            ("list_configs", {"configs_dir": str(config_dir)}),
            ("preview", {"name": "factory-local", "configs_dir": str(config_dir)}),
        ]


@pytest.mark.asyncio
async def test_tui_target_name_uses_selected_registry_target(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
    )
    requested_target_names: list[str] = []
    requested_targets: list[TargetConfig] = []

    class FakeTargetsRegistry:
        def by_name(self, name: str) -> TargetConfig:
            requested_target_names.append(name)
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    class FactoryTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/factory/configs/remote.yaml",
                            "name": "factory-remote",
                            "model": "org/factory-remote",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "factory-remote",
                                "target": "blackbird",
                                "model": "org/factory-remote",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "cwd=/remote\nvllm serve org/factory-remote", "warnings": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target setup should not subscribe")

    def fake_target_client_for_config(target, **_kwargs):
        requested_targets.append(target)
        return FactoryTargetClient()

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(tui_app_module, "target_client_for_config", fake_target_client_for_config)
    app = VllmLoaderApp(configs_dir=config_dir, target_name="blackbird")

    async with app.run_test() as pilot:
        await pilot.pause()

        assert requested_target_names == ["blackbird"]
        assert requested_targets == [blackbird]
        assert app.current_config is not None
        assert app.current_config.name == "factory-remote"


@pytest.mark.asyncio
async def test_tui_select_config_refreshes_preview_through_target_client(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class HandleRefusingAgent:
        def handle(self, method: str, _params=None):
            raise AssertionError(f"direct TUI handle call: {method}")

    client_instances: list[object] = []

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, str]]] = []
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return RecordingConfigAgent().handle(method, params)
            if method == "preview":
                name = str(params["name"])
                return {"preview": f"cwd=/agent\nvllm serve org/{name}", "warnings": []}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("preview refresh should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(HandleRefusingAgent()),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("beta")
        await _wait_for_condition(
            lambda: "vllm serve org/beta" in app.selected_config_preview,
            "selected config preview did not refresh",
        )

        assert app.current_config is not None
        assert app.current_config.name == "beta"
        assert app.current_config.target == "blackbird"
        assert client_instances[0].calls[-1] == (
            "preview",
            {"name": "beta", "configs_dir": str(config_dir)},
        )


@pytest.mark.asyncio
async def test_tui_launch_preparation_runs_through_target_client(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class HandleRefusingAgent:
        def handle(self, method: str, _params=None):
            raise AssertionError(f"direct TUI handle call: {method}")

    client_instances: list[object] = []

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, str]]] = []
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method in {"list_configs", "preview"}:
                return RecordingConfigAgent().handle(method, params)
            if method == "preflight":
                return {
                    "ok": False,
                    "failures": [
                        {"kind": "MODEL_NOT_FOUND", "detail": "agent-side missing model"}
                    ],
                }
            if method == "prepare_launch":
                raise AssertionError("prepare_launch should not run after preflight failure")
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("prepare should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(HandleRefusingAgent()),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.MODEL_NOT_FOUND
        assert "agent-side missing model" in app.error_text
        assert client_instances[0].calls[-1] == (
            "preflight",
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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, _params)
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
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )
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
async def test_tui_launch_passes_build_model_revision_overrides(
    config_dir: Path, tmp_path: Path
) -> None:
    class OverrideLaunchAgent(RecordingConfigAgent):
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

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "launch":
                return {
                    "run_id": "run-override",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "probe_until_ready":
                return {
                    "run_id": "run-override",
                    "ready": True,
                    "detail": "ready from target client",
                    "models": ["served"],
                    "error_kind": None,
                }
            if method == "wait":
                return {"run_id": "run-override", "returncode": 0, "intentional": False}
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "run-override",
                "returncode": 0,
                "intentional": False,
                "phase": Phase.STOPPED.value,
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-override"]
            assert resume_from == "live"
            return self._events()

    agent = OverrideLaunchAgent()
    target_client = FakeTargetClient(agent)
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=target_client,
        launch_overrides={
            "build_id": "01BUILD",
            "model_ref": "01MODEL",
            "revision": "abc123",
        },
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

    assert agent.calls[-1] == (
        "prepare_launch",
        {
            "name": "alpha",
            "configs_dir": str(config_dir),
            "build_id": "01BUILD",
            "model_ref": "01MODEL",
            "revision": "abc123",
        },
    )
    launch_call = next(call for call in target_client.calls if call[0] == "launch")
    assert launch_call[1]["name"] == "alpha"
    assert launch_call[1]["configs_dir"] == str(config_dir)
    assert launch_call[1]["build_id"] == "01BUILD"
    assert launch_call[1]["model_ref"] == "01MODEL"
    assert launch_call[1]["revision"] == "abc123"
    assert launch_call[1]["run_id"]


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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
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

    agent = AgentProfileLaunchAgent()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()
        await pilot.pause()

        calls = _non_discovery_target_calls(app)
        assert calls[0][0] == "launch"
        assert calls[0][1]["name"] == "alpha"
        assert calls[0][1]["configs_dir"] == str(config_dir)
        assert isinstance(calls[0][1]["run_id"], str)
        assert calls[0][1]["run_id"]
        assert calls[1:] == [
            ("probe_until_ready", {"run_id": "run-1"}),
            ("wait", {"run_id": "run-1"}),
        ]
        assert app.current_run_id is None
        assert app.log_lines[-1] == "INFO Starting to load model"
        assert app.phase is Phase.ERROR


@pytest.mark.asyncio
async def test_tui_attached_launch_uses_wait_phase_without_controller_exit_fsm(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LaunchAgent(RecordingConfigAgent):
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

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(LaunchAgent(), method, params)
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
                    "detail": "ready",
                    "models": ["served"],
                    "error_kind": None,
                }
            if method == "wait":
                return {
                    "run_id": "run-1",
                    "returncode": 0,
                    "intentional": False,
                    "phase": Phase.STOPPED.value,
                }
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": 0,
                "intentional": False,
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            assert resume_from == "live"
            return self._events()

    def refuse_controller_exit_fsm(
        self, returncode: int | None, *, intentional: bool = False
    ) -> None:
        raise AssertionError("TUI should consume the serialized wait phase")

    monkeypatch.setattr(
        tui_app_module.PhaseFSM,
        "process_exited",
        refuse_controller_exit_fsm,
    )

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

        assert app.phase is Phase.STOPPED


@pytest.mark.asyncio
async def test_tui_attached_launch_uses_wait_error_metadata_without_terminal_event(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LaunchAgent(RecordingConfigAgent):
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

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(LaunchAgent(), method, params)
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "probe_until_ready":
                return {
                    "run_id": "run-1",
                    "ready": False,
                    "detail": "not ready",
                    "models": [],
                    "error_kind": None,
                }
            if method == "wait":
                return {
                    "run_id": "run-1",
                    "returncode": 7,
                    "intentional": False,
                    "phase": Phase.ERROR.value,
                    "error_kind": ErrorKind.CRASHED.value,
                    "error_excerpt": "process exited with code 7",
                }
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": 7,
                "intentional": False,
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            assert resume_from == "live"
            return self._events()

    def refuse_controller_exit_fsm(
        self, returncode: int | None, *, intentional: bool = False
    ) -> None:
        raise AssertionError("TUI should consume the serialized wait error metadata")

    monkeypatch.setattr(
        tui_app_module.PhaseFSM,
        "process_exited",
        refuse_controller_exit_fsm,
    )

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is ErrorKind.CRASHED
        assert "CRASHED" in app.error_text
        assert "process exited with code 7" in app.error_text


@pytest.mark.asyncio
async def test_tui_attached_launch_subscribes_before_probe(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    class LaunchAgent(RecordingConfigAgent):
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

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "probe_until_ready":
                order.append("probe")
                assert order == ["subscribe", "probe"]
                return {
                    "run_id": "run-1",
                    "ready": True,
                    "detail": "ready",
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
            order.append("subscribe")
            return self._events()

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(LaunchAgent()),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

        assert order == ["subscribe", "probe"]


@pytest.mark.asyncio
async def test_tui_detached_launch_runs_through_target_client(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DetachedLaunchAgent(RecordingConfigAgent):
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

        def start_detached_run(self, *_args, **_kwargs):
            raise AssertionError("direct detached TUI launch")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "discover_runs":
                return {"runs": []}
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "detached",
                    "status": "started",
                }
            if method == "reattach":
                return {
                    "run_id": params["run_id"],
                    "config": {
                        "name": "detached",
                        "model": "org/detached",
                        "launch": {
                            "mode": "detached",
                            "runs_dir": str(tmp_path / "runs"),
                        },
                    },
                    "sidecar": {
                        "config_name": "detached",
                        "host": "127.0.0.1",
                        "port": 8000,
                        "exposure": "local",
                        "served_model_names": ["org/detached"],
                        "launch_mode": "detached",
                        "vllm_version_profile": "current",
                    },
                    "fsm": {"vllm_version_profile": "current"},
                }
            if method == "probe_until_ready":
                return {
                    "run_id": params["run_id"],
                    "ready": True,
                    "detail": "ready",
                    "models": ["org/detached"],
                    "error_kind": None,
                }
            if method == "tail_detached":
                return {"run_id": params["run_id"], "status": "ended"}
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": None,
                "intentional": False,
                "phase": Phase.READY.value,
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            assert resume_from == "live"
            return self._events()

    agent = DetachedLaunchAgent()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_selected_config()

        calls = _non_discovery_target_calls(app)
        assert calls[0][0] == "launch"
        assert calls[0][1]["name"] == "detached"
        assert calls[0][1]["configs_dir"] == str(config_dir)
        assert isinstance(calls[0][1]["run_id"], str)
        assert calls[0][1]["run_id"]
        assert calls[1] == ("reattach", {"run_id": "run-1"})
        assert app.reattached_run_id == "run-1"


@pytest.mark.asyncio
async def test_command_palette_discovers_detached_runs_through_target_client(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DiscoveryAgent(RecordingConfigAgent):
        def discover_detached_runs(self, *_args, **_kwargs):
            raise AssertionError("direct detached discovery")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "discover_runs":
                return {"runs": [{"run_id": "run-1", "config_name": "detached"}]}
            if method == "reattach":
                return {
                    "run_id": params["run_id"],
                    "config": {
                        "name": "detached",
                        "model": "fake/model",
                        "server": {"host": "0.0.0.0", "port": 8000, "exposure": "lan"},
                        "launch": {"mode": "detached"},
                    },
                    "sidecar": {
                        "config_name": "detached",
                        "host": "0.0.0.0",
                        "port": 8000,
                        "exposure": "lan",
                        "served_model_names": ["served"],
                        "launch_mode": "detached",
                        "vllm_version_profile": "current",
                        "reachable_url": "http://127.0.0.1:8000",
                    },
                    "fsm": {"vllm_version_profile": "current"},
                }
            if method == "probe_until_ready":
                return {
                    "run_id": params["run_id"],
                    "ready": True,
                    "detail": "ready",
                    "models": ["served"],
                    "error_kind": None,
                }
            if method == "tail_detached":
                return {"run_id": params["run_id"], "status": "ended"}
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": None,
                "intentional": False,
                "phase": Phase.READY.value,
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            assert resume_from == "live"
            return self._events()

    agent = DiscoveryAgent()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )

    async with app.run_test():
        command = await _wait_for_command(app, "Reattach detached run: detached")
        command.callback()
        await _wait_for_condition(
            lambda: ("reattach", {"run_id": "run-1"})
            in app._target_client.calls,
            "target client reattach was not requested",
        )

        discovery_calls = [
            call for call in app._target_client.calls if call[0] == "discover_runs"
        ]
        assert discovery_calls[0] == (
            "discover_runs",
            {},
        )
        assert app.reattached_run_id == "run-1"
        assert app.ready_url == "http://127.0.0.1:8000"

        notifications: list[str] = []
        load_workers: list[str] = []

        def capture_worker(coro, **kwargs):
            load_workers.append(str(kwargs.get("name", "")))
            coro.close()

        monkeypatch.setattr(
            app,
            "notify",
            lambda message, **_kwargs: notifications.append(str(message)),
        )
        monkeypatch.setattr(app, "run_worker", capture_worker)

        app.action_load()

        assert notifications == ["A detached run is already attached"]
        assert "load" not in load_workers

        app.action_detach()

        assert app.reattached_run_id is None


@pytest.mark.asyncio
async def test_tui_stop_attached_run_signals_target_client_by_run_id(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StopRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            raise AssertionError(f"direct attached TUI liveness check: {run_id}")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "stop":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("stop should not subscribe")

    agent = StopRefusingAgent()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"

        app.action_stop()
        await _wait_for_condition(
            lambda: _non_discovery_target_calls(app)
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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "probe_until_ready":
                return {
                    "run_id": params["run_id"],
                    "ready": True,
                    "detail": "ready from target client",
                    "models": ["served"],
                    "error_kind": None,
                    "reachable_url": "http://10.25.0.51:18123",
                    "phase": Phase.READY.value,
                }
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("direct probe should not subscribe")

    agent = ProbeRefusingAgent()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )
    monkeypatch.setattr(
        tui_app_module.PhaseFSM,
        "health_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("TUI should apply the agent health phase")
        ),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_config is not None
        app.current_run_id = "run-1"

        await app._probe_until_ready(app.current_config)
        await pilot.pause()

        assert _non_discovery_target_calls(app) == [
            ("probe_until_ready", {"run_id": "run-1"})
        ]
        assert app.phase is Phase.READY
        assert app.served_models == ["served"]
        assert app.ready_url == "http://10.25.0.51:18123"


@pytest.mark.asyncio
async def test_tui_detached_health_probe_runs_through_target_client(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProbeRefusingAgent(StopRecordingAgent):
        async def probe_run_until_ready(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct reattached TUI probe")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "probe_until_ready":
                return {
                    "run_id": params["run_id"],
                    "ready": True,
                    "detail": "ready from target client",
                    "models": ["served"],
                    "error_kind": None,
                    "phase": Phase.READY.value,
                }
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("reattached probe should not subscribe")

    agent = ProbeRefusingAgent()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )
    monkeypatch.setattr(
        tui_app_module.PhaseFSM,
        "health_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("TUI should apply the agent health phase")
        ),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_config is not None

        await app._target_probe_run_until_ready("run-1")
        await pilot.pause()

        assert _non_discovery_target_calls(app) == [
            ("probe_until_ready", {"run_id": "run-1"})
        ]
        assert app.phase is Phase.READY
        assert app.served_models == ["served"]


@pytest.mark.asyncio
async def test_tui_consumes_serialized_run_events(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._post_wire_event_message(
            {
                "event": "log",
                "run_id": "run-1",
                "kind": "committed",
                "text": "INFO Starting to load model",
                "level": "INFO",
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }
        )
        app._post_wire_event_message(
            {
                "event": "phase",
                "run_id": "run-1",
                "phase": Phase.LOADING_WEIGHTS.value,
                "prev_phase": Phase.IDLE.value,
                "seq": 2,
                "ts": "2026-06-03T00:00:01Z",
                "mono": 2.0,
            }
        )
        await pilot.pause()

        assert app.log_lines[-1] == "INFO Starting to load model"
        assert app.phase is Phase.LOADING_WEIGHTS


@pytest.mark.asyncio
async def test_tui_resumes_run_event_subscription_from_last_sequence(
    config_dir: Path,
) -> None:
    class SequencedTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.resume_from_values: list[object] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        async def ping(self):
            return {
                "pong": True,
                "target": "local",
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        async def _events(self):
            subscribe_index = len(self.resume_from_values)
            if subscribe_index == 1:
                yield {
                    "event": "log",
                    "run_id": "run-1",
                    "kind": "committed",
                    "text": "first stream",
                    "level": "INFO",
                    "seq": 4,
                    "ts": "2026-06-03T00:00:00Z",
                    "mono": 1.0,
                }
                yield {
                    "event": "exited",
                    "run_id": "run-1",
                    "returncode": 0,
                    "intentional": False,
                    "phase": Phase.STOPPED.value,
                    "seq": 5,
                    "ts": "2026-06-03T00:00:01Z",
                    "mono": 2.0,
                }
                return
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": 0,
                "intentional": False,
                "phase": Phase.STOPPED.value,
                "seq": 6,
                "ts": "2026-06-03T00:00:02Z",
                "mono": 3.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            self.resume_from_values.append(resume_from)
            return self._events()

    target_client = SequencedTargetClient()
    app = VllmLoaderApp(configs_dir=config_dir, target_client=target_client)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert await app._consume_target_run_events_until_exit("run-1") is Phase.STOPPED
        assert await app._consume_target_run_events_until_exit("run-1") is Phase.STOPPED

        assert target_client.resume_from_values == ["live", {"seq": 5}]


@pytest.mark.asyncio
async def test_tui_drops_event_resume_sequence_after_agent_restart(
    config_dir: Path,
) -> None:
    class RestartingSequencedTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.connect_calls = 0
            self.resume_from_values: list[object] = []

        async def connect(self):
            self.connect_calls += 1
            self.connected = True
            return {
                "agent_version": "test",
                "protocol_version": 1,
                "target": "local",
                "daemon_start_ts": (
                    "2026-06-03T00:00:00Z"
                    if self.connect_calls == 1
                    else "2026-06-03T00:01:00Z"
                ),
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        async def ping(self):
            return {
                "pong": True,
                "target": "local",
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": 0,
                "intentional": False,
                "phase": Phase.STOPPED.value,
                "seq": 5 if len(self.resume_from_values) == 1 else 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            self.resume_from_values.append(resume_from)
            return self._events()

    target_client = RestartingSequencedTargetClient()
    app = VllmLoaderApp(configs_dir=config_dir, target_client=target_client)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert await app._consume_target_run_events_until_exit("run-1") is Phase.STOPPED
        await app._mark_target_disconnected("test disconnect")
        assert await app._consume_target_run_events_until_exit("run-1") is Phase.STOPPED

        assert target_client.resume_from_values == ["live", "live"]


@pytest.mark.asyncio
async def test_tui_uses_log_offset_resume_after_agent_restart(
    config_dir: Path,
) -> None:
    class RestartingOffsetTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.connect_calls = 0
            self.resume_from_values: list[object] = []

        async def connect(self):
            self.connect_calls += 1
            self.connected = True
            return {
                "agent_version": "test",
                "protocol_version": 1,
                "target": "local",
                "daemon_start_ts": (
                    "2026-06-03T00:00:00Z"
                    if self.connect_calls == 1
                    else "2026-06-03T00:01:00Z"
                ),
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        async def ping(self):
            return {
                "pong": True,
                "target": "local",
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }

        async def _events(self):
            if len(self.resume_from_values) == 1:
                yield {
                    "event": "log",
                    "run_id": "run-1",
                    "kind": "committed",
                    "text": "first daemon",
                    "level": "INFO",
                    "seq": 5,
                    "log_inode": 77,
                    "byte_offset": 32,
                    "ts": "2026-06-03T00:00:00Z",
                    "mono": 1.0,
                }
                yield {
                    "event": "exited",
                    "run_id": "run-1",
                    "returncode": 0,
                    "intentional": False,
                    "phase": Phase.STOPPED.value,
                    "seq": 6,
                    "ts": "2026-06-03T00:00:01Z",
                    "mono": 2.0,
                }
                return
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": 0,
                "intentional": False,
                "phase": Phase.STOPPED.value,
                "seq": 1,
                "ts": "2026-06-03T00:00:02Z",
                "mono": 3.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            self.resume_from_values.append(resume_from)
            return self._events()

    target_client = RestartingOffsetTargetClient()
    app = VllmLoaderApp(configs_dir=config_dir, target_client=target_client)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert await app._consume_target_run_events_until_exit("run-1") is Phase.STOPPED
        await app._mark_target_disconnected("test disconnect")
        assert await app._consume_target_run_events_until_exit("run-1") is Phase.STOPPED

        assert target_client.resume_from_values == [
            "live",
            {"log_inode": 77, "byte_offset": 32},
        ]


@pytest.mark.asyncio
async def test_wire_phase_timing_uses_agent_monotonic_clock(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir, clock=lambda: 1_000.0)
    async with app.run_test() as pilot:
        await pilot.pause()

        app._post_wire_event_message(
            {
                "event": "phase",
                "run_id": "run-1",
                "phase": Phase.SERVER_STARTING.value,
                "prev_phase": Phase.IDLE.value,
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 10.0,
            }
        )
        app._post_wire_event_message(
            {
                "event": "phase",
                "run_id": "run-1",
                "phase": Phase.LOADING_WEIGHTS.value,
                "prev_phase": Phase.SERVER_STARTING.value,
                "seq": 2,
                "ts": "2026-06-03T00:00:07Z",
                "mono": 17.0,
            }
        )
        await pilot.pause()

        assert app.run_started_at == 10.0
        assert app.current_phase_started_at == 17.0
        assert app.phase_elapsed[Phase.SERVER_STARTING] == 7.0


@pytest.mark.asyncio
async def test_wire_ready_uses_agent_reachable_url_without_phase_mutation(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "remote-ready.yaml",
        """
        name: remote-ready
        model: org/remote
        server:
          host: 127.0.0.1
          port: 8126
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("remote-ready")
        app._set_phase(Phase.SERVER_STARTING)

        app._post_wire_event_message(
            {
                "event": "ready",
                "run_id": "run-1",
                "models": ["served"],
                "reachable_url": "http://10.25.0.51:18003",
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 12.0,
            }
        )
        await pilot.pause()

        assert app.ready_url == "http://10.25.0.51:18003"
        assert app.served_models == ["served"]
        assert app.phase is Phase.SERVER_STARTING


@pytest.mark.asyncio
async def test_wire_health_ready_uses_agent_reachable_url_without_phase_mutation(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "remote-health.yaml",
        """
        name: remote-health
        model: org/remote
        server:
          host: 0.0.0.0
          port: 8127
          exposure: lan
        """,
    )
    app = VllmLoaderApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("remote-health")
        app._set_phase(Phase.SERVER_STARTING)

        app._post_wire_event_message(
            {
                "event": "health",
                "run_id": "run-1",
                "ready": True,
                "detail": "ready",
                "models": ["served"],
                "reachable_url": "http://10.25.0.51:18004",
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 12.0,
            }
        )
        await pilot.pause()

        assert app.ready_url == "http://10.25.0.51:18004"
        assert app.served_models == ["served"]
        assert app.phase is Phase.SERVER_STARTING


@pytest.mark.asyncio
async def test_remote_target_rewrites_loopback_health_url_to_target_host(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
    )

    class FakeTargetsRegistry:
        def by_name(self, name: str) -> TargetConfig:
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    class FakeTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/blackbird/configs/remote-health.yaml",
                            "name": "remote-health",
                            "model": "org/remote",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "remote-health",
                                "target": "blackbird",
                                "model": "org/remote",
                                "server": {
                                    "host": "0.0.0.0",
                                    "port": 18003,
                                    "exposure": "lan",
                                },
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "cwd=/remote\nvllm serve org/remote", "warnings": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("ready URL test should not subscribe")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(),
    )
    app = VllmLoaderApp(configs_dir=config_dir, target_name="blackbird")

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("remote-health")
        app._set_phase(Phase.SERVER_STARTING)

        app._post_wire_event_message(
            {
                "event": "health",
                "run_id": "run-1",
                "ready": True,
                "detail": "ready",
                "models": ["served"],
                "reachable_url": "http://127.0.0.1:18003",
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 12.0,
            }
        )
        await pilot.pause()

        assert app.ready_url == "http://10.25.0.51:18003"
        assert app.phase is Phase.SERVER_STARTING


@pytest.mark.asyncio
async def test_tui_gpu_sampling_runs_through_target_client(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class GpuRefusingAgent(RecordingConfigAgent):
        def sample_gpus(self) -> GpuPollResult:
            raise AssertionError("direct TUI GPU sampling")

    client_instances: list[object] = []

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, object] | None]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []
            self.events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "gpu":
                await self.events.put(
                    {
                        "event": "gpu",
                        "run_id": "__agent__",
                        "sub_id": "gpu-panel",
                        "seq": 1,
                        "ts": "2026-06-03T00:00:00+00:00",
                        "mono": 1.0,
                        "samples": [
                            {
                                "visible_index": 0,
                                "uuid": "GPU-a",
                                "name": "A100",
                                "memory_used_mb": 1024,
                                "memory_total_mb": 81920,
                                "utilization_percent": 25,
                                "temperature_c": 42,
                                "power_w": 110,
                                "mig_instance_id": None,
                            }
                        ],
                        "note": "",
                        "unavailable": False,
                    }
                )
                return {"sub_id": str(params["sub_id"])}
            if method == "unsubscribe":
                return {"sub_id": str(params["sub_id"])}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            self.subscribe_calls.append((list(run_ids), resume_from))

            async def events():
                while True:
                    yield await self.events.get()

            return events()

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(GpuRefusingAgent()),
        gpu_interval_seconds=60,
    )

    async with app.run_test():
        await _wait_for_condition(
            lambda: any(method == "gpu" for method, _params in client_instances[0].calls),
            "GPU stream was not started",
        )
        await _wait_for_condition(
            lambda: "A100" in app.gpu_panel_text,
            "GPU push event was not rendered",
        )

        assert client_instances[0].subscribe_calls == [(["__agent__"], "live")]
        assert (
            "gpu",
            {"sub_id": "gpu-panel", "interval_s": 60},
        ) in client_instances[0].calls
        assert (
            "gpu",
            {"emit_event": True, "sub_id": "gpu-panel"},
        ) not in client_instances[0].calls
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
        assert "b Builds" in str(help_text.content)
        assert "m Models" in str(help_text.content)
        assert "F Flags" in str(help_text.content)
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
async def test_confirm_kill_attached_run_signals_target_client_by_run_id(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class KillRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            raise AssertionError(f"direct attached TUI liveness check: {run_id}")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "kill":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("kill should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(KillRefusingAgent()),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"

        await pilot.press("K")
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: _non_discovery_target_calls(app) == [("kill", {"run_id": "run-1"})],
            "target client kill was not requested",
        )

        assert app.screen.id != "confirm"


@pytest.mark.asyncio
async def test_kill_confirm_names_active_target(config_dir: Path) -> None:
    class TargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target-name confirm should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=TargetClient(),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"

        await pilot.press("K")
        await pilot.pause()

        assert app.screen.id == "confirm"
        message = app.screen.query_one("#confirm-message", Static)
        assert "on blackbird" in message.content.plain


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
async def test_config_picker_uses_agent_preview_without_controller_profile(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
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
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/alpha --agent-preview",
                    "warnings": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("config picker preview should not subscribe")

    def refuse_controller_profile(_cfg):
        raise AssertionError("Config picker should use agent preview cache")

    monkeypatch.setattr(
        config_picker_module,
        "select_profile_for_config",
        refuse_controller_profile,
        raising=False,
    )
    app = VllmLoaderApp(configs_dir=config_dir, target_client=TargetClient())

    async with app.run_test() as pilot:
        await pilot.pause()
        await _wait_for_condition(
            lambda: "--agent-preview" in app.selected_config_preview,
            "agent preview was not cached",
        )
        await pilot.press("c")
        await pilot.pause()

        assert "Resolved command" in app.screen.summary
        assert "vllm serve org/alpha --agent-preview" in app.screen.summary


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
async def test_flag_manager_opens_from_binding_and_partitions_flags(
    config_dir: Path,
    tmp_path: Path,
) -> None:
    fake_vllm = tmp_path / "fake-vllm"
    fake_vllm.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('vllm 0.11.2')\n"
        "elif len(sys.argv) >= 2 and sys.argv[1] == 'serve':\n"
        "    print('usage: vllm serve')\n"
        "    print('  --host TEXT')\n"
        "    print('  --port INTEGER')\n"
        "    print('  --tensor-parallel-size INTEGER')\n"
        "    print('  --kv-cache-dtype TEXT')\n"
        "    print('  --moe-backend TEXT')\n",
        encoding="utf-8",
    )
    fake_vllm.chmod(0o755)
    write_yaml(
        config_dir / "flags.yaml",
        f"""
        name: flags
        model: org/model
        command:
          executable: {fake_vllm}
        engine:
          tensor_parallel_size: 2
          kv_cache_dtype: fp8
        extra_args:
          - --moe-backend
          - flashinfer_cutlass
          - --legacy-flag
          - value
        """,
    )
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        await pilot.pause()

        assert app.screen.id == "flag-manager"
        flag_list = str(app.screen.query_one("#flag-manager-list", Static).content)
        detail = str(app.screen.query_one("#flag-manager-detail", Static).content)
        assert "Flag Manager" in flag_list
        assert "modeled 2" in flag_list
        assert "passthrough 1" in flag_list
        assert "unknown 1" in flag_list
        assert "MODELED" in flag_list
        assert "kv-cache-dtype = fp8" in flag_list
        assert "tensor-parallel-size = 2" in flag_list
        assert "PASSTHROUGH" in flag_list
        assert "--moe-backend flashinfer_cutlass" in flag_list
        assert "UNKNOWN-TO-BUILD" in flag_list
        assert "--legacy-flag value" in flag_list
        assert "Resolved command" in detail
        assert "--kv-cache-dtype fp8" in detail
        assert "--moe-backend flashinfer_cutlass" in detail


@pytest.mark.asyncio
async def test_flag_manager_uses_agent_flag_map_for_modeled_flags(
    config_dir: Path,
) -> None:
    class TargetClient:
        connected = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "capabilities": [
                    "list_configs",
                    "preview",
                    "update_config_flags",
                    "gpu",
                    "discover_runs",
                ]
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/flags.yaml",
                            "name": "flags",
                            "model": "org/model",
                            "target": None,
                            "warnings": [],
                            "config": {
                                "name": "flags",
                                "model": "org/model",
                                "engine": {"tensor_parallel_size": 2},
                                "extra_args": [
                                    "--remote-known",
                                    "value",
                                    "--local-only",
                                    "value",
                                ],
                            },
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": (
                        "cwd=/agent\n"
                        "vllm serve org/model --tp-remote 2 --remote-known value"
                    ),
                    "warnings": [],
                    "metadata": {
                        "known_flags": ["--tp-remote", "--remote-known"],
                        "flag_map": {"tensor_parallel_size": "--tp-remote"},
                    },
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("flag manager should not subscribe")

    app = VllmLoaderApp(configs_dir=config_dir, target_client=TargetClient())

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await _wait_for_condition(
            lambda: "--tp-remote" in app.selected_config_preview,
            "agent preview metadata was not loaded",
        )
        await pilot.press("F")
        await _wait_for_condition(
            lambda: app.screen.id == "flag-manager",
            "flag manager did not open",
        )

        flag_list = str(app.screen.query_one("#flag-manager-list", Static).content)
        assert "tp-remote = 2" in flag_list
        assert "tensor-parallel-size = 2" not in flag_list
        assert "--remote-known value" in flag_list
        assert "--local-only value" in flag_list


@pytest.mark.asyncio
async def test_flag_manager_reset_modeled_flag_saves_to_config(
    config_dir: Path,
    tmp_path: Path,
) -> None:
    fake_vllm = tmp_path / "fake-vllm"
    fake_vllm.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('vllm 0.11.2')\n"
        "elif len(sys.argv) >= 2 and sys.argv[1] == 'serve':\n"
        "    print('usage: vllm serve')\n"
        "    print('  --host TEXT')\n"
        "    print('  --port INTEGER')\n"
        "    print('  --tensor-parallel-size INTEGER')\n"
        "    print('  --kv-cache-dtype TEXT')\n",
        encoding="utf-8",
    )
    fake_vllm.chmod(0o755)
    config_path = config_dir / "flags-save.yaml"
    write_yaml(
        config_path,
        f"""
        name: flags-save
        model: org/model
        command:
          executable: {fake_vllm}
        engine:
          tensor_parallel_size: 2
          kv_cache_dtype: fp8
        """,
    )
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        await _wait_for_condition(
            lambda: app.screen.id == "flag-manager",
            "flag manager did not open",
        )
        await pilot.press("down")
        await pilot.press("d")
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "_default",
            "flag manager did not close after save",
        )

        assert app.current_config is not None
        assert app.current_config.name == "flags-save"
        assert app.current_config.engine.tensor_parallel_size == 2
        assert app.current_config.engine.kv_cache_dtype is None
        text = config_path.read_text(encoding="utf-8")
        assert "tensor_parallel_size: 2" in text
        assert "kv_cache_dtype" not in text
        assert "--kv-cache-dtype" not in app.selected_config_preview


@pytest.mark.asyncio
async def test_flag_manager_editing_modeled_flag_refreshes_agent_preview(
    config_dir: Path,
) -> None:
    class TargetClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object] | None]] = []

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "capabilities": [
                    "list_configs",
                    "preview",
                    "update_config_flags",
                    "gpu",
                    "discover_runs",
                ]
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/flags-edit.yaml",
                            "name": "flags-edit",
                            "model": "org/model",
                            "target": None,
                            "warnings": [],
                            "config": {
                                "name": "flags-edit",
                                "model": "org/model",
                                "engine": {"tensor_parallel_size": 2},
                            },
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                engine = params.get("engine") if isinstance(params, dict) else None
                tensor_parallel_size = (
                    engine.get("tensor_parallel_size")
                    if isinstance(engine, dict)
                    else 2
                )
                return {
                    "preview": (
                        "cwd=/agent\n"
                        f"vllm serve org/model --tensor-parallel-size {tensor_parallel_size}"
                    ),
                    "warnings": [],
                    "metadata": {
                        "known_flags": ["--tensor-parallel-size"],
                        "flag_map": {"tensor_parallel_size": "--tensor-parallel-size"},
                    },
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("flag manager should not subscribe")

    target_client = TargetClient()
    app = VllmLoaderApp(configs_dir=config_dir, target_client=target_client)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        await _wait_for_condition(
            lambda: app.screen.id == "flag-manager",
            "flag manager did not open",
        )

        value_input = app.screen.query_one("#flag-manager-value", Input)
        value_input.value = "4"
        await _wait_for_condition(
            lambda: "--tensor-parallel-size 4"
            in str(app.screen.query_one("#flag-manager-detail", Static).content),
            "flag manager preview did not refresh with edited value",
        )

    preview_calls = [
        params
        for method, params in target_client.calls
        if method == "preview" and isinstance(params, dict)
    ]
    assert any(
        isinstance(params.get("engine"), dict)
        and params["engine"].get("tensor_parallel_size") == "4"
        for params in preview_calls
    )


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
        assert "F Flags" in _static_text(app, "#footer-bindings")
        assert "Tab Focus" in _static_text(app, "#footer-bindings")
        assert "^P Palette" in _static_text(app, "#footer-bindings")

        status = app.query_one("#status")
        status_strip = app.query_one("#status-strip")
        footer = app.query_one("#footer-bindings")
        assert status.region.height >= 3
        assert status_strip.region.y + status_strip.region.height <= footer.region.y


@pytest.mark.asyncio
async def test_header_uses_agent_preview_metadata_for_build_model_scope(
    config_dir: Path,
) -> None:
    class PreviewMetadataTargetClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/composed.yaml",
                            "name": "composed",
                            "model": "meta-llama/Llama-3.1-8B-Instruct",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "composed",
                                "target": "blackbird",
                                "model": "meta-llama/Llama-3.1-8B-Instruct",
                                "model_ref": "llama-pin",
                                "revision": "abc123",
                                "command": {"build": "nightly-cu130"},
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve meta-llama/Llama-3.1-8B-Instruct",
                    "warnings": [],
                    "metadata": {
                        "build_id": "01BUILD",
                        "build_label": "nightly-cu130",
                        "vllm_version": "0.17.0.dev",
                        "model_ref": "llama-pin",
                        "model_display_name": "llama-pin",
                        "model_cache_state": "cached",
                        "model_revision": "abc123",
                    },
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("header preview metadata should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=PreviewMetadataTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.pause()

        segment = _static_text(app, "#active-model")
        assert "▣ 📌nightly-cu130 ●" in segment
        assert "M 📌llama-pin ● abc123" in segment
        assert "Target: blackbird" in app.config_summary
        assert "Build: ▣ 📌nightly-cu130 ●" in app.config_summary
        assert "Model state: M 📌llama-pin ● abc123" in app.config_summary


@pytest.mark.asyncio
async def test_build_manager_selects_build_through_target_client(
    config_dir: Path,
) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.active_build = "stable-cu124"
            self.select_calls: list[str] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_id": (
                            "01NIGHTLY"
                            if self.active_build == "nightly-cu130"
                            else "01STABLE"
                        ),
                        "build_label": self.active_build,
                        "build_status": "ready",
                        "model_display_name": "buildable",
                    },
                }
            if method == "list_builds":
                return {
                    "default_build_id": (
                        "01NIGHTLY"
                        if self.active_build == "nightly-cu130"
                        else "01STABLE"
                    ),
                    "builds": [
                        {
                            "build_id": "01STABLE",
                            "label": "stable-cu124",
                            "status": "ready",
                            "default": self.active_build == "stable-cu124",
                            "resolved": {"vllm": "0.11.2", "cuda": "12.4"},
                            "paths": {"executable": "bin/vllm"},
                        },
                        {
                            "build_id": "01NIGHTLY",
                            "label": "nightly-cu130",
                            "status": "ready",
                            "default": self.active_build == "nightly-cu130",
                            "resolved": {"vllm": "0.17.0.dev", "cuda": "13.0"},
                            "paths": {"executable": "bin/vllm"},
                        },
                    ],
                    "skipped": [],
                }
            if method == "select_build":
                self.select_calls.append(str(params["build"]))
                self.active_build = str(params["build"])
                return {"build_id": "01NIGHTLY", "label": self.active_build, "active": True}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("build manager should not subscribe")

    target_client = BuildTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.pause()
        assert "▣ stable-cu124 ●" in _static_text(app, "#active-model")

        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        build_list = str(app.screen.query_one("#build-manager-list", Static).content)
        assert "> ● stable-cu124  ready  ● active" in build_list
        assert "  ● nightly-cu130  ready" in build_list

        await pilot.press("down")
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: target_client.select_calls == ["nightly-cu130"]
            and "▣ nightly-cu130 ●" in _static_text(app, "#active-model"),
            "build selection did not refresh header",
        )


@pytest.mark.asyncio
async def test_build_manager_flags_binding_opens_flag_manager(config_dir: Path) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_id": "01STABLE",
                        "build_label": "stable-cu124",
                        "build_status": "ready",
                        "model_display_name": "buildable",
                    },
                }
            if method == "list_builds":
                return {
                    "default_build_id": "01STABLE",
                    "builds": [
                        {
                            "build_id": "01STABLE",
                            "label": "stable-cu124",
                            "status": "ready",
                            "default": True,
                            "resolved": {"vllm": "0.11.2", "cuda": "12.4"},
                            "paths": {"executable": "bin/vllm"},
                        },
                    ],
                    "skipped": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("build flags path should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=BuildTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )

        await pilot.press("F")

        await _wait_for_condition(
            lambda: app.screen.id == "flag-manager",
            "flag manager did not open from build manager",
        )


@pytest.mark.asyncio
async def test_build_manager_surfaces_live_build_refs(config_dir: Path) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "live-build",
                        "build_status": "ready",
                        "model_display_name": "buildable",
                    },
                }
            if method == "list_builds":
                return {
                    "default_build_id": "01LIVE",
                    "builds": [
                        {
                            "build_id": "01LIVE",
                            "label": "live-build",
                            "status": "ready",
                            "default": True,
                            "install": {
                                "method": "nightly",
                                "source": "cu130",
                            },
                            "resolved": {"vllm": "0.11.2", "cuda": "12.4"},
                            "paths": {"executable": "bin/vllm"},
                            "in_use": True,
                            "config_refs": ["buildable", "canary"],
                            "config_ref_count": 2,
                            "live_refs": [
                                {
                                    "run_id": "run-live",
                                    "sidecar_path": "/agent/runs/run-live.json",
                                }
                            ],
                        }
                    ],
                    "skipped": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("build manager live-ref test should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=BuildTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        build_list = str(app.screen.query_one("#build-manager-list", Static).content)
        detail = str(app.screen.query_one("#build-manager-detail", Static).content)
        assert "live-build  ready  ● active  🔒 in use  ⇩ used by 2 configs" in build_list
        assert "source: nightly/cu130" in detail
        assert "in_use: 1 live run (run-live)" in detail
        assert "used_by_configs: 2 (buildable, canary)" in detail


@pytest.mark.asyncio
async def test_build_manager_create_build_streams_job_events(
    config_dir: Path,
) -> None:
    class FakeEvents:
        def __init__(self) -> None:
            self.closed = False
            self.job_id = ""
            self._events: list[dict[str, object]] = []

        def arm(self, job_id: str) -> None:
            self.job_id = job_id
            self._events = [
                {
                    "event": "job_progress",
                    "job_id": job_id,
                    "kind": "committed",
                    "text": "Installing build",
                    "level": "INFO",
                },
                {
                    "event": "job_progress",
                    "job_id": job_id,
                    "kind": "transient",
                    "text": "Installing build: 50% 1/2",
                },
                {
                    "event": "job_done",
                    "job_id": job_id,
                    "ok": True,
                    "detail": "build ready",
                },
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

        async def aclose(self) -> None:
            self.closed = True

    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.events = FakeEvents()
            self.check_calls: list[dict[str, object]] = []
            self.create_calls: list[dict[str, object]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "build_status": "ready",
                        "model_display_name": "buildable",
                    },
                }
            if method == "list_builds":
                return {
                    "default_build_id": "01STABLE",
                    "builds": [
                        {
                            "build_id": "01STABLE",
                            "label": "stable-cu124",
                            "status": "ready",
                            "default": True,
                            "resolved": {"vllm": "0.11.2", "cuda": "12.4"},
                            "paths": {"executable": "bin/vllm"},
                        }
                    ],
                    "skipped": [],
                }
            if method == "check_build_prerequisites":
                self.check_calls.append(dict(params))
                return {"ok": True, "method": params["method"], "uv_available": True}
            if method == "create_build":
                self.create_calls.append(dict(params))
                self.events.arm(str(params["job_id"]))
                return {
                    "job_id": params["job_id"],
                    "kind": "create_build",
                    "status": "running",
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            self.subscribe_calls.append((list(run_ids), resume_from))
            if list(run_ids) == ["__agent__"]:
                async def gpu_events():
                    while True:
                        await asyncio.sleep(60)
                        yield {}

                return gpu_events()
            return self.events

    target_client = BuildTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "create-build",
            "create build screen did not open",
        )
        assert not app.screen.query("#create-build-build-id")
        uv_note = str(app.screen.query_one("#create-build-uv-note", Static).content)
        assert "Nightly and commit require uv on the target" in uv_note
        app.screen.query_one("#create-build-method", Select).value = "nightly"
        app.screen.query_one("#create-build-label", Input).value = "nvfp4"
        app.screen.query_one("#create-build-channel", Input).value = "cu130"
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: bool(target_client.create_calls)
            and target_client.events.closed
            and "Installing build" in app.visible_log_lines
            and app.progress_text == "build ready",
            "create build job did not stream through the TUI",
        )
        job_id = str(target_client.create_calls[0]["job_id"])
        assert target_client.check_calls == [
            {
                "method": "nightly",
                "label": "nvfp4",
                "channel": "cu130",
            }
        ]
        assert target_client.create_calls == [
            {
                "job_id": job_id,
                "method": "nightly",
                "label": "nvfp4",
                "channel": "cu130",
            }
        ]
        assert ([job_id], "live") in target_client.subscribe_calls


@pytest.mark.asyncio
async def test_build_manager_rejects_uv_less_target_before_create_job(
    config_dir: Path,
) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/model", "warnings": []}
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method == "check_build_prerequisites":
                raise TargetCallError(
                    "feature-unavailable",
                    "create_build method=nightly requires uv",
                    {"reason": "uv-required", "method": "nightly"},
                )
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            self.subscribe_calls.append((list(run_ids), resume_from))
            if list(run_ids) == ["__agent__"]:
                async def gpu_events():
                    while True:
                        await asyncio.sleep(60)
                        yield {}

                return gpu_events()
            raise AssertionError("create_build should not subscribe before uv precheck")

    target_client = BuildTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)):
        await _wait_for_target_connection_state(app, "connected")
        await app._create_build({"method": "nightly", "channel": "cu130"})

        assert ("check_build_prerequisites", {"method": "nightly", "channel": "cu130"}) in (
            target_client.calls
        )
        assert all(method != "create_build" for method, _params in target_client.calls)
        assert all(run_ids == ["__agent__"] for run_ids, _ in target_client.subscribe_calls)
        assert "requires uv" in app.error_text


@pytest.mark.asyncio
async def test_build_manager_adopts_external_venv_through_target_client(
    config_dir: Path,
) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.adopt_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "build_status": "ready",
                        "model_display_name": "buildable",
                    },
                }
            if method == "list_builds":
                return {
                    "default_build_id": "01STABLE",
                    "builds": [
                        {
                            "build_id": "01STABLE",
                            "label": "stable-cu124",
                            "status": "ready",
                            "default": True,
                            "resolved": {"vllm": "0.11.2", "cuda": "12.4"},
                            "paths": {"executable": "bin/vllm"},
                        }
                    ],
                    "skipped": [],
                }
            if method == "adopt_build":
                self.adopt_calls.append(dict(params))
                return {
                    "build_id": "01ADOPTED",
                    "label": "external-nightly",
                    "status": "adopted",
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("build adopt should not subscribe")

    target_client = BuildTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        await pilot.press("a")
        await _wait_for_condition(
            lambda: app.screen.id == "adopt-build",
            "adopt build screen did not open",
        )
        app.screen.query_one("#adopt-build-label", Input).value = "external-nightly"
        app.screen.query_one("#adopt-build-venv-path", Input).value = (
            "/agent/venvs/vllm-nightly"
        )
        app.screen.query_one("#adopt-build-vllm-version", Input).value = "0.17.0.dev"
        app.screen.query_one("#adopt-build-vllm-version-profile", Input).value = "current"
        app.screen.query_one("#adopt-build-copy", Checkbox).value = True
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: target_client.adopt_calls
            == [
                {
                    "label": "external-nightly",
                    "venv_path": "/agent/venvs/vllm-nightly",
                    "vllm_version": "0.17.0.dev",
                    "vllm_version_profile": "current",
                    "copy": "true",
                }
            ],
            "build adopt was not requested",
        )


@pytest.mark.asyncio
async def test_build_manager_verifies_build_through_target_client(
    config_dir: Path,
) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.verify_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "build_status": "ready",
                        "model_display_name": "buildable",
                    },
                }
            if method == "list_builds":
                return {
                    "default_build_id": "01STABLE",
                    "builds": [
                        {
                            "build_id": "01STABLE",
                            "label": "stable-cu124",
                            "status": "ready",
                            "default": True,
                            "resolved": {"vllm": "0.11.2", "cuda": "12.4"},
                            "paths": {"executable": "bin/vllm"},
                        }
                    ],
                    "skipped": [],
                }
            if method == "verify_build":
                self.verify_calls.append(dict(params))
                return {
                    "build_id": "01STABLE",
                    "label": "stable-cu124",
                    "status": "ready",
                    "ok": True,
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("build verify should not subscribe")

    target_client = BuildTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        await pilot.press("v")

        await _wait_for_condition(
            lambda: target_client.verify_calls == [{"build": "stable-cu124"}],
            "build verify was not requested",
        )


@pytest.mark.asyncio
async def test_build_manager_repairs_build_through_target_client(
    config_dir: Path,
) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.repair_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "repair-me",
                        "build_status": "broken",
                        "model_display_name": "buildable",
                    },
                }
            if method == "list_builds":
                return {
                    "default_build_id": None,
                    "builds": [
                        {
                            "build_id": "01BROKEN",
                            "label": "repair-me",
                            "status": "broken",
                            "default": False,
                            "resolved": {"vllm": "0.11.2", "cuda": "12.4"},
                            "paths": {"executable": "bin/vllm"},
                        }
                    ],
                    "skipped": [],
                }
            if method == "repair_build":
                self.repair_calls.append(dict(params))
                return {
                    "build_id": "01BROKEN",
                    "label": "repair-me",
                    "status": "ready",
                    "ok": True,
                    "detail": "build repaired",
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("build repair should not subscribe")

    target_client = BuildTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        await pilot.press("r")

        await _wait_for_condition(
            lambda: target_client.repair_calls == [{"build": "repair-me"}],
            "build repair was not requested",
        )


@pytest.mark.asyncio
async def test_build_manager_remove_confirms_and_calls_target_client(
    config_dir: Path,
) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.remove_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/buildable.yaml",
                            "name": "buildable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "buildable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "build_status": "ready",
                        "model_display_name": "buildable",
                    },
                }
            if method == "list_builds":
                return {
                    "default_build_id": "01STABLE",
                    "builds": [
                        {
                            "build_id": "01STABLE",
                            "label": "stable-cu124",
                            "status": "ready",
                            "default": True,
                            "paths": {"executable": "/agent/builds/01STABLE/bin/vllm"},
                        },
                        {
                            "build_id": "01OLD",
                            "label": "old-cu124",
                            "status": "ready",
                            "default": False,
                            "paths": {"executable": "/agent/builds/01OLD/bin/vllm"},
                        },
                    ],
                    "skipped": [],
                }
            if method == "remove_build":
                self.remove_calls.append(dict(params))
                return {
                    "build_id": "01OLD",
                    "label": "old-cu124",
                    "removed": True,
                    "removed_path": "/agent/builds/01OLD",
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("build remove should not subscribe")

    target_client = BuildTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        await pilot.press("down")
        await pilot.press("x")
        await _wait_for_condition(
            lambda: app.screen.id == "confirm",
            "build remove confirm did not open",
        )
        confirm_text = str(app.screen.query_one("#confirm-message", Static).content)
        assert "Remove build old-cu124 on blackbird?" in confirm_text
        assert "on blackbird" in confirm_text
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: bool(target_client.remove_calls),
            "build remove was not requested",
        )
        assert target_client.remove_calls == [
            {"build": "old-cu124", "configs_dir": str(config_dir)}
        ]


@pytest.mark.asyncio
async def test_model_manager_opens_model_catalog_from_target_client(
    config_dir: Path,
) -> None:
    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "model_display_name": "modelable",
                    },
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "01MODEL",
                            "display_name": "llama-pin",
                            "source": "hf_repo",
                            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "quant_format": "awq",
                            "cache_state": "cached",
                            "gated": True,
                            "size_bytes": 16_060_530_000,
                            "unique_size_bytes": 2_100_000_000,
                            "nominal_size_bytes": 16_060_530_000,
                            "files": {"count": 7, "weights_format": "safetensors"},
                        },
                        {
                            "entry_id": "02REMOTE",
                            "display_name": "qwen-remote",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3-32B",
                            "revision": "main",
                            "commit_sha": None,
                            "quant_format": "bf16",
                            "cache_state": "remote_only",
                            "gated": False,
                            "size_bytes": 0,
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model manager should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=ModelTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )

        model_list = str(app.screen.query_one("#model-manager-list", Static).content)
        detail = str(app.screen.query_one("#model-manager-detail", Static).content)
        assert "> ● llama-pin  awq  2.1 GB unique / 16.1 GB nominal @abc123 🔒" in model_list
        assert "  ○ qwen-remote  bf16  -- @main" in model_list
        assert "repo: meta-llama/Llama-3.1-8B-Instruct" in detail
        assert "revision: main → abc123" in detail
        assert "size: 2.1 GB unique / 16.1 GB nominal" in detail
        assert "auth: gated, requires HF_TOKEN" in detail
        assert "files: 7 safetensors" in detail


@pytest.mark.asyncio
async def test_model_manager_enter_selects_model_for_active_config(
    config_dir: Path,
) -> None:
    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.preview_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                call = dict(params)
                self.preview_calls.append(call)
                metadata: dict[str, object] = {"build_label": "stable-cu124"}
                if call.get("model_ref") == "02REMOTE":
                    metadata.update(
                        {
                            "model_ref": "02REMOTE",
                            "model_display_name": "qwen-remote",
                            "model_revision": "main",
                            "model_cache_state": "remote_only",
                        }
                    )
                else:
                    metadata["model_display_name"] = "modelable"
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": metadata,
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "01MODEL",
                            "display_name": "llama-pin",
                            "source": "hf_repo",
                            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "quant_format": "awq",
                            "cache_state": "cached",
                            "files": {},
                        },
                        {
                            "entry_id": "02REMOTE",
                            "display_name": "qwen-remote",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3-32B",
                            "revision": "main",
                            "commit_sha": None,
                            "quant_format": "bf16",
                            "cache_state": "remote_only",
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model select should not subscribe")

    target_client = ModelTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )

        await pilot.press("down")
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: any(call.get("model_ref") == "02REMOTE" for call in target_client.preview_calls)
            and "qwen-remote" in _static_text(app, "#active-model"),
            "model selection did not refresh active config preview",
        )
        assert target_client.preview_calls[-1]["model_ref"] == "02REMOTE"
        assert target_client.preview_calls[-1]["revision"] == "main"
        assert "M qwen-remote ○ main" in _static_text(app, "#active-model")


@pytest.mark.asyncio
async def test_model_manager_marks_url_models_launch_time_only(
    config_dir: Path,
) -> None:
    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "capabilities": [
                    "list_configs",
                    "preview",
                    "list_models",
                    "download_model",
                    "gpu",
                    "discover_runs",
                ]
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {},
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "01URL",
                            "display_name": "url-gguf",
                            "source": "url",
                            "url": "https://models.example/Qwen/example-q4.gguf",
                            "quant_format": "gguf",
                            "cache_state": "remote_only",
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "download_model":
                raise AssertionError("URL model should not start a download job")
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("URL model manager should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=ModelTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )
        detail = str(app.screen.query_one("#model-manager-detail", Static).content)
        assert "download: launch-time-only" in detail

        await pilot.press("d")
        await _wait_for_condition(
            lambda: "launch-time-only" in app.error_text,
            "URL model download was not blocked with launch-time-only guidance",
        )


@pytest.mark.asyncio
async def test_model_manager_refreshes_catalog_through_target_client(
    config_dir: Path,
) -> None:
    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.refresh_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "model_display_name": "qwen-remote",
                    },
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "02REMOTE",
                            "display_name": "qwen-remote",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3-32B",
                            "revision": "main",
                            "commit_sha": None,
                            "quant_format": "bf16",
                            "cache_state": "remote_only",
                            "gated": False,
                            "size_bytes": 0,
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "refresh_models":
                self.refresh_calls.append(dict(params))
                return {
                    "refreshed": 1,
                    "models": [
                        {
                            "entry_id": "02REMOTE",
                            "display_name": "qwen-remote",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3-32B",
                            "revision": "main",
                            "commit_sha": None,
                            "quant_format": "bf16",
                            "cache_state": "cached",
                            "gated": False,
                            "size_bytes": 0,
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model refresh should not subscribe")

    target_client = ModelTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )
        await pilot.press("r")

        await _wait_for_condition(
            lambda: target_client.refresh_calls == [{}]
            and app.screen.id == "model-manager",
            "model refresh was not requested",
        )
        model_list = str(app.screen.query_one("#model-manager-list", Static).content)
        assert "> ● qwen-remote  bf16  -- @main" in model_list


@pytest.mark.asyncio
async def test_model_manager_download_streams_job_events(
    config_dir: Path,
) -> None:
    class FakeEvents:
        def __init__(self) -> None:
            self.closed = False
            self.job_id = ""
            self._events: list[dict[str, object]] = []

        def arm(self, job_id: str) -> None:
            self.job_id = job_id
            self._events = [
                {
                    "event": "job_progress",
                    "job_id": job_id,
                    "kind": "committed",
                    "text": "Downloading model",
                    "level": "INFO",
                },
                {
                    "event": "job_progress",
                    "job_id": job_id,
                    "kind": "transient",
                    "text": "Downloading weights: 50% 1/2",
                },
                {
                    "event": "job_done",
                    "job_id": job_id,
                    "ok": True,
                    "detail": "model cached",
                },
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

        async def aclose(self) -> None:
            self.closed = True

    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.events = FakeEvents()
            self.download_calls: list[dict[str, object]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "model_display_name": "llama-pin",
                    },
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "01MODEL",
                            "display_name": "llama-pin",
                            "source": "hf_repo",
                            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "quant_format": "awq",
                            "cache_state": "remote_only",
                            "gated": False,
                            "size_bytes": 0,
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "download_model":
                self.download_calls.append(dict(params))
                self.events.arm(str(params["job_id"]))
                return {
                    "job_id": params["job_id"],
                    "kind": "download_model",
                    "status": "running",
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            self.subscribe_calls.append((list(run_ids), resume_from))
            if list(run_ids) == ["__agent__"]:
                async def gpu_events():
                    while True:
                        await asyncio.sleep(60)
                        yield {}

                return gpu_events()
            return self.events

    target_client = ModelTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )
        await pilot.press("d")
        await _wait_for_condition(
            lambda: app.screen.id == "download-model",
            "model download screen did not open",
        )
        app.screen.query_one("#download-model-revision", Input).value = "main"
        app.screen.query_one("#download-model-allow", Input).value = "*.safetensors,*.json"
        app.screen.query_one("#download-model-ignore", Input).value = "*.bin"
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: bool(target_client.download_calls)
            and target_client.events.closed
            and "Downloading model" in app.visible_log_lines
            and app.progress_text == "model cached",
            "model download job did not stream through the TUI",
        )
        job_id = str(target_client.download_calls[0]["job_id"])
        assert target_client.download_calls == [
            {
                "job_id": job_id,
                "model_ref": "01MODEL",
                "revision": "main",
                "allow_patterns": ["*.safetensors", "*.json"],
                "ignore_patterns": ["*.bin"],
            }
        ]
        assert ([job_id], "live") in target_client.subscribe_calls


@pytest.mark.asyncio
async def test_stop_cancels_active_model_download_job(
    config_dir: Path,
) -> None:
    class StreamingEvents:
        def __init__(self) -> None:
            self.closed = False
            self.queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

        def arm(self, job_id: str) -> None:
            self.queue.put_nowait(
                {
                    "event": "job_progress",
                    "job_id": job_id,
                    "kind": "committed",
                    "text": "Downloading model",
                    "level": "INFO",
                }
            )

        def finish_cancelled(self, job_id: str) -> None:
            self.queue.put_nowait(
                {
                    "event": "job_done",
                    "job_id": job_id,
                    "ok": False,
                    "error_kind": "cancelled",
                    "detail": "model download cancelled",
                }
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.queue.get()
            if item is None:
                raise StopAsyncIteration
            return item

        async def aclose(self) -> None:
            self.closed = True
            self.queue.put_nowait(None)

    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.events = StreamingEvents()
            self.download_calls: list[dict[str, object]] = []
            self.cancel_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "model_display_name": "llama-pin",
                    },
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "01MODEL",
                            "display_name": "llama-pin",
                            "source": "hf_repo",
                            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "quant_format": "awq",
                            "cache_state": "remote_only",
                            "gated": False,
                            "size_bytes": 0,
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "download_model":
                self.download_calls.append(dict(params))
                self.events.arm(str(params["job_id"]))
                return {
                    "job_id": params["job_id"],
                    "kind": "download_model",
                    "status": "running",
                }
            if method == "cancel_job":
                self.cancel_calls.append(dict(params))
                self.events.finish_cancelled(str(params["job_id"]))
                return {
                    "job_id": params["job_id"],
                    "cancelled": True,
                    "status": "cancelled",
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            if list(run_ids) == ["__agent__"]:
                async def gpu_events():
                    while True:
                        await asyncio.sleep(60)
                        yield {}

                return gpu_events()
            return self.events

    target_client = ModelTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )
        await pilot.press("d")
        await _wait_for_condition(
            lambda: app.screen.id == "download-model",
            "model download screen did not open",
        )
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: bool(target_client.download_calls)
            and "Downloading model" in app.visible_log_lines,
            "model download job did not start",
        )
        job_id = str(target_client.download_calls[0]["job_id"])

        await pilot.press("s")

        await _wait_for_condition(
            lambda: target_client.cancel_calls == [{"job_id": job_id}]
            and app.progress_text == "model download cancelled",
            "active model download job was not cancelled",
        )


@pytest.mark.asyncio
async def test_model_manager_pins_model_metadata_through_target_client(
    config_dir: Path,
) -> None:
    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.pin_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "model_display_name": "qwen-remote",
                    },
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "02REMOTE",
                            "display_name": "qwen-remote",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3-32B",
                            "revision": "main",
                            "commit_sha": None,
                            "quant_format": "bf16",
                            "cache_state": "remote_only",
                            "gated": False,
                            "size_bytes": 0,
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "pin_model":
                self.pin_calls.append(dict(params))
                return {
                    "entry": {
                        "entry_id": "02REMOTE",
                        "display_name": "qwen-remote",
                        "source": "hf_repo",
                        "repo_id": "Qwen/Qwen3-32B",
                    }
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model pin should not subscribe")

    target_client = ModelTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )
        await pilot.press("p")
        await _wait_for_condition(
            lambda: app.screen.id == "pin-model",
            "pin model screen did not open",
        )
        assert not app.screen.query("#pin-model-entry-id")
        app.screen.query_one("#pin-model-repo-id", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#pin-model-display-name", Input).value = "qwen-remote"
        app.screen.query_one("#pin-model-revision", Input).value = "main"
        app.screen.query_one("#pin-model-commit-sha", Input).value = "abc123"
        app.screen.query_one("#pin-model-quant-format", Input).value = "bf16"
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: bool(target_client.pin_calls),
            "model pin was not requested",
        )
        assert target_client.pin_calls == [
            {
                "repo_id": "Qwen/Qwen3-32B",
                "display_name": "qwen-remote",
                "revision": "main",
                "commit_sha": "abc123",
                "quant_format": "bf16",
            }
        ]


@pytest.mark.asyncio
async def test_model_manager_pins_url_model_metadata_through_target_client(
    config_dir: Path,
) -> None:
    model_url = "https://models.example/Qwen/example-q4.gguf"

    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.pin_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "model_display_name": "url-gguf",
                    },
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "02URL",
                            "display_name": "url-gguf",
                            "source": "url",
                            "url": model_url,
                            "revision": None,
                            "commit_sha": None,
                            "quant_format": "q4_k_m",
                            "tokenizer": "Qwen/Qwen3-32B",
                            "notes": "operator note",
                            "cache_state": "remote_only",
                            "gated": True,
                            "token_required": True,
                            "size_bytes": 0,
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "pin_model":
                self.pin_calls.append(dict(params))
                return {
                    "entry": {
                        "entry_id": "02URL",
                        "display_name": "url-gguf",
                        "source": "url",
                        "url": model_url,
                    }
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model pin should not subscribe")

    target_client = ModelTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )
        await pilot.press("p")
        await _wait_for_condition(
            lambda: app.screen.id == "pin-model",
            "pin model screen did not open",
        )

        assert app.screen.query_one("#pin-model-url", Input).value == model_url
        assert app.screen.query_one("#pin-model-tokenizer", Input).value == "Qwen/Qwen3-32B"
        assert app.screen.query_one("#pin-model-notes", Input).value == "operator note"
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: bool(target_client.pin_calls),
            "url model pin was not requested",
        )
        assert target_client.pin_calls == [
            {
                "url": model_url,
                "source": "url",
                "display_name": "url-gguf",
                "quant_format": "q4_k_m",
                "tokenizer": "Qwen/Qwen3-32B",
                "gated": True,
                "token_required": True,
                "notes": "operator note",
            }
        ]


@pytest.mark.asyncio
async def test_model_manager_verifies_model_through_target_client(
    config_dir: Path,
) -> None:
    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.verify_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "model_display_name": "llama-pin",
                    },
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "01MODEL",
                            "display_name": "llama-pin",
                            "source": "hf_repo",
                            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "quant_format": "awq",
                            "cache_state": "cached",
                            "gated": False,
                            "size_bytes": 16_060_530_000,
                            "files": {"count": 7, "weights_format": "safetensors"},
                        }
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "verify_model":
                self.verify_calls.append(dict(params))
                return {
                    "entry_id": "01MODEL",
                    "ok": True,
                    "cache_state": "cached",
                    "detail": "model metadata is cached",
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model verify should not subscribe")

    target_client = ModelTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )
        await pilot.press("v")

        await _wait_for_condition(
            lambda: target_client.verify_calls == [{"model_ref": "01MODEL"}],
            "model verify was not requested",
        )


@pytest.mark.asyncio
async def test_model_manager_remove_confirms_and_calls_target_client(
    config_dir: Path,
) -> None:
    class ModelTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.remove_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/modelable.yaml",
                            "name": "modelable",
                            "model": "org/model",
                            "target": "blackbird",
                            "warnings": [],
                            "config": {
                                "name": "modelable",
                                "target": "blackbird",
                                "model": "org/model",
                            },
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "build_label": "stable-cu124",
                        "model_display_name": "llama-pin",
                    },
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "01MODEL",
                            "display_name": "llama-pin",
                            "source": "hf_repo",
                            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "quant_format": "awq",
                            "cache_state": "cached",
                            "gated": False,
                            "size_bytes": 16_060_530_000,
                            "files": {"count": 7, "weights_format": "safetensors"},
                        },
                        {
                            "entry_id": "02REMOTE",
                            "display_name": "qwen-remote",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3-32B",
                            "revision": "main",
                            "commit_sha": None,
                            "quant_format": "bf16",
                            "cache_state": "remote_only",
                            "gated": False,
                            "size_bytes": 0,
                            "files": {},
                        },
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "remove_model":
                self.remove_calls.append(dict(params))
                return {
                    "entry_id": "02REMOTE",
                    "source": "hf_repo",
                    "removed_weights": False,
                    "entry": {"display_name": "qwen-remote"},
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model remove should not subscribe")

    target_client = ModelTargetClient()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open",
        )
        await pilot.press("down")
        await pilot.press("x")
        await _wait_for_condition(
            lambda: app.screen.id == "confirm",
            "model remove confirm did not open",
        )
        confirm_text = str(app.screen.query_one("#confirm-message", Static).content)
        assert "Remove model qwen-remote on blackbird?" in confirm_text
        assert "on blackbird" in confirm_text
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: bool(target_client.remove_calls),
            "model remove was not requested",
        )
        assert target_client.remove_calls == [
            {"model_ref": "02REMOTE", "configs_dir": str(config_dir)}
        ]


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

        app._post_wire_event_message(
            {
                "event": "gpu",
                "run_id": "__agent__",
                "sub_id": "gpu-panel",
                "seq": 1,
                "ts": "2026-06-03T00:00:00+00:00",
                "mono": 1.0,
                "samples": [
                    {
                        "visible_index": 0,
                        "uuid": "GPU-a",
                        "name": "A100",
                        "memory_used_mb": 1024,
                        "memory_total_mb": 81920,
                        "utilization_percent": 25,
                        "temperature_c": 42,
                        "power_w": 110,
                        "mig_instance_id": None,
                    }
                ],
                "note": "",
                "unavailable": False,
            }
        )
        await pilot.pause()
        assert "A100" in app.gpu_panel_text
        assert "1024/81920MB" in app.gpu_panel_text

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

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent(gpu_sampler=sampler)),
        gpu_interval_seconds=0.05,
    )

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

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent(gpu_sampler=sampler)),
        gpu_interval_seconds=0.01,
    )

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

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent(gpu_sampler=sampler)),
        gpu_interval_seconds=60,
    )

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
                error_kind=ErrorKind.API_KEY_AUTH,
            )
        )

        assert app.phase is Phase.ERROR
        assert "API_KEY_AUTH" in app.error_text
        assert "VLLM_API_KEY" in app.error_text
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
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
    )

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [TargetConfig(name="local"), blackbird]

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
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
            "Manage targets",
            "Switch target: blackbird",
            "Agent info",
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
            assert app.reattached_run_id == launch.run_id
            assert any("Uvicorn running" in line for line in app.log_lines)
    finally:
        await _cleanup_port(port)


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
        app.reattached_run_id = "run-1"
        app.action_load()

        assert worker_calls == []
        assert notifications[-1] == "A detached run is already attached"


@pytest.mark.asyncio
async def test_stop_after_agent_reattach_signals_target_client_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StopRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            raise AssertionError(f"direct reattached TUI liveness check: {run_id}")

        def stop_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct reattached TUI stop")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "stop":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("reattached stop should not subscribe")

    cancelled_groups: list[str] = []

    def cancel_group(_app: VllmLoaderApp, group: str) -> None:
        cancelled_groups.append(group)

    agent = StopRefusingAgent()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app.workers, "cancel_group", cancel_group)
        app.reattached_run_id = "run-1"

        app.action_stop()
        await _wait_for_condition(
            lambda: _non_discovery_target_calls(app)
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
            "target client reattached stop was not requested",
        )

        assert cancelled_groups[-2:] == ["tail", "health"]
        assert app.reattached_run_id is None
        assert app.phase is Phase.STOPPED


@pytest.mark.asyncio
async def test_kill_after_agent_reattach_signals_target_client_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class KillRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            raise AssertionError(f"direct reattached TUI liveness check: {run_id}")

        def kill_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct reattached TUI kill")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "kill":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("reattached kill should not subscribe")

    cancelled_groups: list[str] = []

    def cancel_group(_app: VllmLoaderApp, group: str) -> None:
        cancelled_groups.append(group)

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(KillRefusingAgent()),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app.workers, "cancel_group", cancel_group)
        app.reattached_run_id = "run-1"

        await pilot.press("K")
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: _non_discovery_target_calls(app) == [("kill", {"run_id": "run-1"})],
            "target client reattached kill was not requested",
        )

        assert cancelled_groups[-2:] == ["tail", "health"]
        assert app.reattached_run_id is None
        assert app.phase is Phase.STOPPED


@pytest.mark.asyncio
async def test_target_reattach_error_shows_error_without_crashing(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_target_client = _fake_reattach_target_client(
        error=TargetCallError("run-not-found", "unknown detached run: run-1")
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: fake_target_client(),
    )
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()

        await app._reattach_target_detached_run("run-1")

        assert "Unable to reattach" in app.error_text
        assert "run-1" in app.error_text


def test_tui_does_not_expose_path_based_detached_reattach() -> None:
    assert "reattach_detached_run" not in VllmLoaderApp.__dict__


def test_tui_does_not_compute_target_runs_dirs() -> None:
    assert "_runs_dirs" not in VllmLoaderApp.__dict__


def test_tui_constructor_only_accepts_target_client_boundary() -> None:
    params = inspect.signature(VllmLoaderApp).parameters

    assert "target_client" in params
    assert "target_name" in params
    assert "agent" not in params
    assert "gpu_sampler" not in params


def test_tui_does_not_store_attached_process_handle(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    assert "current_process" not in app.__dict__


def test_tui_does_not_store_reattached_sidecar_path(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    assert "reattached_sidecar_path" not in app.__dict__


@pytest.mark.asyncio
async def test_reattach_health_worker_is_non_crashing_monitor(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_target_client = _fake_reattach_target_client(
        _target_reattach_payload(served_model_names=["fake-model"])
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: fake_target_client(),
    )
    worker_calls: list[dict[str, object]] = []

    def capture_worker(coro, **kwargs):
        worker_calls.append(kwargs)
        coro.close()

    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        await app._reattach_target_detached_run("run-1")

        health_worker = next(
            call for call in worker_calls if call["name"] == "reattach-health"
        )
        assert health_worker["group"] == "health"
        assert health_worker["exit_on_error"] is False


@pytest.mark.asyncio
async def test_reattach_starts_tail_worker_before_health_probe(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_target_client = _fake_reattach_target_client(
        _target_reattach_payload(served_model_names=["fake-model"])
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: fake_target_client(),
    )
    worker_names: list[str] = []

    def capture_worker(coro, **kwargs):
        worker_names.append(str(kwargs.get("name")))
        coro.close()
        return SimpleNamespace()

    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        await app._reattach_target_detached_run("run-1")

        assert worker_names[:2] == ["reattach-tail", "reattach-health"]


@pytest.mark.asyncio
async def test_reattach_health_snapshot_updates_phase_when_stream_misses_ready(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeTargetClient:
        def __init__(self) -> None:
            self.agent = LocalAgent()
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "", "unavailable": False}
            if method == "reattach":
                return _target_reattach_payload(served_model_names=["fake-model"])
            if method == "probe_until_ready":
                return {
                    "run_id": params["run_id"],
                    "ready": True,
                    "detail": "ready",
                    "models": ["fake-model"],
                    "error_kind": None,
                    "reachable_url": "http://127.0.0.1:8000",
                    "phase": Phase.READY.value,
                }
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("stream should not be required for ready snapshot")

    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(),
    )
    app = VllmLoaderApp(configs_dir=config_dir)
    worker_coros: dict[str, object] = {}

    def capture_worker(coro, **kwargs):
        worker_coros[str(kwargs.get("name"))] = coro
        return SimpleNamespace()

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        await app._reattach_target_detached_run("run-1")
        worker_coros["reattach-tail"].close()
        await worker_coros["reattach-health"]
        await pilot.pause()

        assert app.phase is Phase.READY
        assert app.ready_url == "http://127.0.0.1:8000"


@pytest.mark.asyncio
async def test_reattach_hydrates_copyable_url_and_models_from_sidecar(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_target_client = _fake_reattach_target_client(
        _target_reattach_payload(
            host="0.0.0.0",
            port=8123,
            exposure="lan",
            reachable_url="http://10.25.0.51:8123",
            served_model_names=["sidecar-model"],
        )
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: fake_target_client(),
    )

    def capture_worker(coro, **_kwargs):
        coro.close()

    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        await app._reattach_target_detached_run("run-1")

        assert app.ready_url == "http://10.25.0.51:8123"
        assert app.served_models == ["sidecar-model"]
        assert app._server_url_for_copy() == "http://10.25.0.51:8123"
        assert app.phase is Phase.SERVER_STARTING


@pytest.mark.asyncio
async def test_remote_target_rewrites_loopback_reattach_url_to_target_host(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
    )

    class FakeTargetsRegistry:
        def by_name(self, name: str) -> TargetConfig:
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    fake_target_client = _fake_reattach_target_client(
        _target_reattach_payload(
            host="0.0.0.0",
            port=8123,
            exposure="lan",
            reachable_url="http://127.0.0.1:8123",
            served_model_names=["sidecar-model"],
        )
    )
    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: fake_target_client(),
    )

    def capture_worker(coro, **_kwargs):
        coro.close()

    app = VllmLoaderApp(configs_dir=config_dir, target_name="blackbird")

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        await app._reattach_target_detached_run("run-1")

        assert app.ready_url == "http://10.25.0.51:8123"
        assert app.served_models == ["sidecar-model"]


@pytest.mark.asyncio
async def test_reattach_restores_registry_secrets_missing_from_sidecar_snapshot(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
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
    fake_target_client = _fake_reattach_target_client(
        _target_reattach_payload(
            config_name="secret-detached",
            model="snapshot/model",
            served_model_names=["snapshot-model"],
            config_extra={"server": {"host": "127.0.0.1", "port": 8000}, "env": {}},
        )
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: fake_target_client(),
    )

    def capture_worker(coro, **_kwargs):
        coro.close()

    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        await app._reattach_target_detached_run("run-1")

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
            await _reattach_discovered_target_run(app, launch.run_id)
            await _wait_for_phase(app, Phase.READY)
            await pilot.press("s")
            await _wait_for_port_down(port)
    finally:
        await _cleanup_port(port)


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
            await _reattach_discovered_target_run(app, launch.run_id)
            await _wait_for_phase(app, Phase.READY)

            commands = list(app.get_system_commands(app.screen))
            assert "Detach from detached run" in {command.title for command in commands}

            app.action_detach()

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
            launched_run_id = app.reattached_run_id
            await pilot.press("s")
            await _wait_for_port_down(port)
            assert launched_run_id is not None
            sidecar_paths = [
                path
                for path in runs_dir.glob("*.json")
                if not path.name.endswith(".manifest.json")
            ]
            assert len(sidecar_paths) == 1
            sidecar = json.loads(sidecar_paths[0].read_text(encoding="utf-8"))
            assert sidecar["vllm_version_profile"] == "older-request-logging-on"
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_tui_detached_tail_consumes_agent_events(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TailAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            return False

        async def tail_detached_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct reattached TUI tail")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "tail_detached":
                return {"run_id": params["run_id"], "status": "ended"}
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
                "returncode": None,
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

    agent = TailAgent()
    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        await app._target_tail_detached_run("run-1", start_position=123)
        await pilot.pause()

        assert _non_discovery_target_calls(app) == [
            ("tail_detached", {"run_id": "run-1", "start_position": 123})
        ]
        assert app.log_lines[-1] == "INFO Starting to load model"
        assert app.phase is Phase.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "excerpt", "guidance"),
    [
        (
            ErrorKind.OOM,
            "CUDA out of memory while profiling KV cache",
            "gpu_memory_utilization",
        ),
        (ErrorKind.CRASHED, "INFO Starting to load model", "last log lines"),
    ],
)
async def test_wire_phase_error_shows_named_banner(
    config_dir: Path, kind: ErrorKind, excerpt: str, guidance: str
) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._post_wire_event_message(
            {
                "event": "phase",
                "run_id": "run-1",
                "phase": Phase.ERROR.value,
                "prev_phase": Phase.LOADING_WEIGHTS.value,
                "error_kind": kind.value,
                "error_excerpt": excerpt,
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }
        )
        await pilot.pause()

        assert app.phase is Phase.ERROR
        assert app.fsm.error_kind is kind
        assert kind.value in app.error_text
        assert excerpt in app.error_text
        assert guidance in app.error_text


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
    src_path = Path.cwd() / "src"
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
        env:
          PYTHONPATH: "{src_path}"
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
    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_log(app, "Uvicorn running")
            await _wait_for_phase(app, Phase.READY)
            await pilot.press("s")
            await _wait_for_stopped(app)
    finally:
        await _cleanup_port(port)

    durable_logs = list(runs_dir.glob("*.run.log"))
    assert len(durable_logs) == 1
    durable_log = durable_logs[0]
    assert durable_log.exists()
    assert "Uvicorn running" in durable_log.read_text(encoding="utf-8")
    assert "checkpoint shards" not in durable_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_force_kill_running_attached_server_is_intentional_stop(
    config_dir: Path,
) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    src_path = Path.cwd() / "src"
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
        env:
          PYTHONPATH: "{src_path}"
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
async def test_restart_attached_run_signals_target_client_by_run_id(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "alpha.yaml",
        """
        name: alpha
        model: org/alpha
        """,
    )

    class RestartRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            raise AssertionError(f"direct attached TUI liveness check: {run_id}")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "restart":
                return {
                    "run_id": params["run_id"],
                    "new_run_id": "run-2",
                    "status": "started",
                    "launch": {
                        "run_id": "run-2",
                        "status": "started",
                        "launch_mode": "attached",
                    },
                }
            if method == "stop":
                raise AssertionError("restart should use the restart RPC")
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("restart RPC test should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(RestartRefusingAgent()),
    )
    load_calls: list[str | None] = []
    monitor_calls: list[tuple[str | None, str]] = []

    async def fake_monitor_attached_run(cfg: object, run_id: str) -> None:
        monitor_calls.append((getattr(cfg, "name", None), run_id))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = load_registry(config_dir).by_name("alpha")
        app.current_run_id = "run-1"
        monkeypatch.setattr(app, "action_load", lambda: load_calls.append(app.current_run_id))
        monkeypatch.setattr(
            app,
            "_monitor_attached_run",
            fake_monitor_attached_run,
            raising=False,
        )

        app.action_restart()
        await _wait_for_condition(
            lambda: len(_non_discovery_target_calls(app)) == 1,
            "target client restart RPC was not requested",
        )

        method, params = _non_discovery_target_calls(app)[0]
        assert method == "restart"
        assert params["run_id"] == "run-1"
        assert isinstance(params["new_run_id"], str)
        assert params["new_run_id"] != "run-1"
        assert params["name"] == "alpha"
        assert params["configs_dir"] == str(config_dir)
        assert params["interrupt_timeout"] == 2
        assert params["terminate_timeout"] == 2
        assert load_calls == []
        await _wait_for_condition(
            lambda: monitor_calls == [("alpha", "run-2")],
            "restart did not monitor the agent-started replacement run",
        )


@pytest.mark.asyncio
async def test_restart_after_agent_detached_reattach_signals_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "restart-detached.yaml",
        """
        name: restart-detached
        model: org/model
        """,
    )
    class RestartRefusingAgent(RecordingConfigAgent):
        def stop_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct reattached TUI restart stop")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "restart":
                return {
                    "run_id": params["run_id"],
                    "new_run_id": "run-2",
                    "status": "started",
                    "launch": {
                        "run_id": "run-2",
                        "status": "started",
                        "launch_mode": "attached",
                    },
                }
            if method == "stop":
                raise AssertionError("reattached restart should use the restart RPC")
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("reattached restart RPC test should not subscribe")

    agent = RestartRefusingAgent()
    load_calls: list[str | None] = []
    monitor_calls: list[tuple[str | None, str]] = []

    async def fake_monitor_attached_run(cfg: object, run_id: str) -> None:
        monitor_calls.append((getattr(cfg, "name", None), run_id))

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(agent),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = load_registry(config_dir).by_name("restart-detached")
        app.reattached_run_id = "run-1"
        app._set_phase(Phase.READY)
        monkeypatch.setattr(
            app,
            "action_load",
            lambda: load_calls.append(app.reattached_run_id),
        )
        monkeypatch.setattr(
            app,
            "_monitor_attached_run",
            fake_monitor_attached_run,
            raising=False,
        )

        app.action_restart()
        await _wait_for_condition(
            lambda: len(_non_discovery_target_calls(app)) == 1,
            "target client reattached restart RPC was not requested",
        )

        method, params = _non_discovery_target_calls(app)[0]
        assert method == "restart"
        assert params["run_id"] == "run-1"
        assert isinstance(params["new_run_id"], str)
        assert params["new_run_id"] != "run-1"
        assert params["name"] == "restart-detached"
        assert params["configs_dir"] == str(config_dir)
        assert params["interrupt_timeout"] == 2
        assert params["terminate_timeout"] == 2
        assert load_calls == []
        await _wait_for_condition(
            lambda: monitor_calls == [("restart-detached", "run-2")],
            "restart did not monitor the agent-started replacement run",
        )
        assert app.reattached_run_id is None


@pytest.mark.asyncio
async def test_restart_after_target_detached_reattach(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "restart-target-detached.yaml",
        """
        name: restart-target-detached
        model: org/model
        """,
    )

    class RestartRefusingAgent(RecordingConfigAgent):
        def stop_run(self, *_args, **_kwargs) -> None:
            raise AssertionError("direct target-reattached TUI restart stop")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "restart":
                return {
                    "run_id": params["run_id"],
                    "new_run_id": "run-2",
                    "status": "started",
                    "launch": {
                        "run_id": "run-2",
                        "status": "started",
                        "launch_mode": "detached",
                    },
                }
            if method == "reattach":
                return {
                    "run_id": params["run_id"],
                    "config": {
                        "name": "restart-target-detached",
                        "model": "org/model",
                    },
                    "sidecar": {
                        "config_name": "restart-target-detached",
                        "server_host": "127.0.0.1",
                        "server_port": 8000,
                        "served_model_names": [],
                    },
                    "fsm": {},
                }
            if method in {"tail_detached", "probe_until_ready"}:
                return {"run_id": params["run_id"], "ready": False}
            if method == "stop":
                raise AssertionError("target-reattached restart should use restart RPC")
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def events():
                if False:
                    yield {}

            return events()

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(RestartRefusingAgent()),
    )
    load_calls: list[str | None] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = load_registry(config_dir).by_name("restart-target-detached")
        app.reattached_run_id = "run-1"
        app._set_phase(Phase.READY)
        monkeypatch.setattr(app, "action_load", lambda: load_calls.append(app.reattached_run_id))

        app.action_restart()
        await _wait_for_condition(
            lambda: any(call[0] == "restart" for call in _non_discovery_target_calls(app)),
            "target client target-reattached restart RPC was not requested",
        )

        method, params = _non_discovery_target_calls(app)[0]
        assert method == "restart"
        assert params["run_id"] == "run-1"
        assert isinstance(params["new_run_id"], str)
        assert params["new_run_id"] != "run-1"
        assert params["name"] == "restart-target-detached"
        assert params["configs_dir"] == str(config_dir)
        assert params["interrupt_timeout"] == 2
        assert params["terminate_timeout"] == 2
        assert load_calls == []
        await _wait_for_condition(
            lambda: app.reattached_run_id == "run-2",
            "restart did not reattach to the agent-started detached replacement run",
        )


@pytest.mark.asyncio
async def test_restart_stops_running_attached_server_and_starts_same_config(
    config_dir: Path,
) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    src_path = Path.cwd() / "src"
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
        env:
          PYTHONPATH: "{src_path}"
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
async def test_quit_confirm_stop_attached_run_signals_target_client_by_run_id(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class QuitStopRefusingAgent(RecordingConfigAgent):
        def is_run_alive(self, run_id: str) -> bool:
            raise AssertionError(f"direct attached TUI liveness check: {run_id}")

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
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "stop":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("quit stop should not subscribe")

    app = VllmLoaderApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(QuitStopRefusingAgent()),
    )
    exit_calls: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))

        app.confirm_stop_running()
        await _wait_for_condition(
            lambda: _non_discovery_target_calls(app)
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


def _non_discovery_target_calls(app: VllmLoaderApp):
    return [
        call
        for call in app._target_client.calls
        if call[0] not in {"discover_runs", "gpu", "sample_gpus", *_TARGET_CONFIG_METHODS}
    ]


def _fake_reattach_target_client(payload: dict | None = None, error: Exception | None = None):
    class FakeTargetClient:
        def __init__(self, agent=None) -> None:
            self.agent = agent or LocalAgent()
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method in _TARGET_CONFIG_METHODS:
                return _delegate_config_target_call(self.agent, method, params)
            if method == "reattach":
                if error is not None:
                    raise error
                assert payload is not None
                return payload
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("reattach test should not subscribe")

    return FakeTargetClient


def _target_reattach_payload(
    *,
    run_id: str = "run-1",
    config_name: str = "fake-child",
    model: str = "fake/model",
    host: str = "127.0.0.1",
    port: int = 8000,
    exposure: str = "local",
    reachable_url: str | None = None,
    served_model_names: list[str] | None = None,
    config_extra: dict | None = None,
) -> dict:
    config = {
        "name": config_name,
        "model": model,
        "server": {"host": host, "port": port, "exposure": exposure},
        "launch": {"mode": "detached"},
    }
    if config_extra:
        config.update(config_extra)
    sidecar = {
        "config_name": config_name,
        "host": host,
        "port": port,
        "exposure": exposure,
        "served_model_names": served_model_names or [model],
        "launch_mode": "detached",
        "vllm_version_profile": "current",
    }
    if reachable_url is not None:
        sidecar["reachable_url"] = reachable_url
    return {
        "run_id": run_id,
        "config": config,
        "sidecar": sidecar,
        "fsm": {"vllm_version_profile": "current"},
    }


async def _reattach_discovered_target_run(app: VllmLoaderApp, run_id: str) -> None:
    await app._refresh_detached_runs()
    assert any(run["run_id"] == run_id for run in app.detached_run_summaries)
    await app._reattach_target_detached_run(run_id)


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


async def _wait_for_target_connection_state(
    app: VllmLoaderApp, state: str, *, timeout: float = 5.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if app.target_connection_state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"target connection state was {app.target_connection_state!r}, expected {state!r}"
    )


async def _wait_for_stopped(app: VllmLoaderApp) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if app.current_run_id is None and app.phase is Phase.STOPPED:
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
