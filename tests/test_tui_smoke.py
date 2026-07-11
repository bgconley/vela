from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import scaled_timeout, write_yaml
from fakes.fake_docker import write_fake_docker_runtime
from rich.cells import cell_len
from rich.text import Text
from textual.color import Color
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, ProgressBar, RichLog, Select, Static
from textual.worker import WorkerState

from vela.agent import local as agent_local_module
from vela.agent.local import LocalAgent, TargetCallError
from vela.config.loader import load_registry
from vela.config.targets import TargetConfig, TransportKind
from vela.engine.command_builder import build_command
from vela.engine.log_sink import LogRecord
from vela.engine.phases import ErrorKind, Phase
from vela.engine.process_manager import start_detached
from vela.messages import (
    AgentError,
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
from vela.monitoring.gpu import GpuPollResult, GpuSample
from vela.monitoring.health import HealthEvent
from vela.transport.inprocess import InProcessTargetClient
from vela.tui import app as tui_app_module
from vela.tui.app import VelaApp
from vela.tui.screens import config_picker as config_picker_module
from vela.tui.screens.confirm import ConfirmScreen


def _optional_wizard_section_result(method: str) -> dict[str, object] | None:
    """Empty-but-present results for the wizard's 3 optional enrichment RPCs.

    Wizard-walk fakes that don't exercise recipe/model/build enrichment delegate
    their ``call`` fallback here so ``_load_new_deployment_sections`` takes the
    CLEAN path (no ``#new-deployment-*-warning`` row) instead of the degraded
    warning-row layout its ``except`` branch renders when the RPC raises
    AssertionError. Returns ``None`` for any other method, so the caller still
    raises on a genuinely unexpected call. Shared so a fake opts in with a
    single fallback line rather than three copy-pasted stubs (A3).
    """
    return {
        "list_deployment_recipes": {"recipes": []},
        "list_models": {"models": []},
        "list_builds": {"builds": [], "skipped": []},
    }.get(method)


def _run_fresh_tui_import(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    subprocess_env = env.copy()
    subprocess_env["PYTHONPATH"] = (
        f"{repo_root / 'src'}{os.pathsep}{subprocess_env.get('PYTHONPATH', '')}"
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os\n"
                "import vela.tui.app\n"
                "from textual import constants\n"
                "print(constants.COLOR_SYSTEM)\n"
                "print(os.environ.get('TEXTUAL_COLOR_SYSTEM', ''))\n"
            ),
        ],
        check=True,
        capture_output=True,
        env=subprocess_env,
        text=True,
    )


def test_tui_import_defaults_to_truecolor_for_figma_palette() -> None:
    env = os.environ.copy()
    env["TERM"] = "dumb"
    env.pop("NO_COLOR", None)
    env.pop("TEXTUAL_COLOR_SYSTEM", None)

    result = _run_fresh_tui_import(env)

    assert result.stdout.splitlines() == ["truecolor", "truecolor"]


def test_tui_import_honors_no_color_opt_out() -> None:
    env = os.environ.copy()
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env.pop("TEXTUAL_COLOR_SYSTEM", None)

    result = _run_fresh_tui_import(env)

    assert result.stdout.splitlines() == ["auto", ""]


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


def test_wire_agent_error_maps_to_visible_agent_message() -> None:
    message = tui_app_module._message_from_wire_event(
        {
            "event": "agent_error",
            "detail": "Malformed NDJSON frame from target agent",
            "fatal": False,
            "ts": "2026-06-03T00:00:00Z",
            "mono": 1.0,
        }
    )

    assert isinstance(message, AgentError)
    assert message.detail == "Malformed NDJSON frame from target agent"
    assert message.fatal is False


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

    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir, target_client=target_client)

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

    app = VelaApp(
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
        workdir=Path("/tank/repos/vela"),
        venv=Path("/tank/venvs/vela"),
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
    app = VelaApp(
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
        assert "workdir: /tank/repos/vela" in detail
        assert "venv: /tank/venvs/vela" in detail
        assert "connection: connected" in detail
        assert "agent: 0.9.0-agent" in detail
        assert "controller: 0.9.0-controller" in detail
        assert "protocol: 1" in detail
        assert "capabilities: gpu, health, list_configs, prepare_launch, preview" in detail
        assert "active_runs: 2 (alpha, beta)" in detail
        assert "gpu: 0 A100 1024/81920MB 25%" in detail
        assert "last_seen:" in detail


@pytest.mark.asyncio
async def test_target_manager_live_refreshes_on_reconnect_while_open(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Task 4.2 / bug-237: a Reconnect fired from the OPEN manager must flip its
    # frozen constructor snapshot to the honest live state in place — the detail
    # shows `reconnecting…` immediately, then the app's reconnect-completion path
    # calls refresh_target_state so the manager reflects the reconnected agent
    # WITHOUT the modal closing. Event-gated so both moments are observable.
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        workdir=Path("/tank/repos/vela"),
        venv=Path("/tank/venvs/vela"),
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

    gate = asyncio.Event()

    class GatedReconnectClient:
        def __init__(self) -> None:
            self.connected = False
            self.connects = 0

        async def connect(self) -> dict[str, object]:
            self.connects += 1
            if self.connects >= 2:
                # The reconnect blocks here so the `reconnecting…` moment is
                # observable; it returns an UPDATED agent version to prove the
                # manager re-rendered from fresh state, not the stale snapshot.
                await gate.wait()
            self.connected = True
            version = "1.0.0-agent" if self.connects >= 2 else "0.9.0-agent"
            return {
                "agent_version": version,
                "controller_version": "0.9.0-controller",
                "agent_protocol_version": 1,
                "protocol_version": 1,
                "target": "blackbird",
                "daemon_start_ts": "2026-06-03T00:00:00Z",
                "capabilities": ["list_configs", "preview", "gpu", "health"],
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

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target manager should not subscribe")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=GatedReconnectClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("t")
        await pilot.pause()
        assert app.screen.id == "target-manager"
        detail = str(app.screen.query_one("#target-manager-detail", Static).content)
        assert "agent: 0.9.0-agent" in detail  # the initial snapshot

        # Fire the app's reconnect from the open manager (what `R` delegates to).
        app.screen.action_reconnect()
        await pilot.pause()
        # Moment 1: optimistic reconnecting… while the worker is gated, still open.
        detail = str(app.screen.query_one("#target-manager-detail", Static).content)
        assert "connection: reconnecting…" in detail
        assert app.screen.id == "target-manager"

        # Release the gated reconnect; the completion path live-refreshes in place.
        gate.set()
        await _wait_for_target_connection_state(app, "connected")
        await _wait_for_textual_condition(
            pilot,
            lambda: "agent: 1.0.0-agent"
            in str(app.screen.query_one("#target-manager-detail", Static).content),
            "target manager did not live-refresh after reconnect",
        )
        # Moment 2: the reconnected agent shows, and the modal never closed.
        assert app.screen.id == "target-manager"
        detail = str(app.screen.query_one("#target-manager-detail", Static).content)
        assert "connection: connected" in detail
        assert "agent: 1.0.0-agent" in detail


@pytest.mark.asyncio
async def test_target_manager_failed_reconnect_renders_truthfully(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-257: a FAILED reconnect fired from the open manager must NOT leave the
    # modal stuck at the optimistic `reconnecting…`. _ensure_target_client_connected
    # re-raises on failure, killing the worker BEFORE the tail refresh — the
    # try/finally in _reconnect_target guarantees the truthful failed state
    # (`unreachable`) is still pushed into the open modal, matching the chrome.
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        workdir=Path("/tank/repos/vela"),
        venv=Path("/tank/venvs/vela"),
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

    class DyingAgentClient:
        def __init__(self) -> None:
            self.connected = False
            self.connects = 0

        async def connect(self) -> dict[str, object]:
            self.connects += 1
            if self.connects >= 2:
                # The agent died between the startup connect and the R press —
                # the most likely real reason an operator reaches for Reconnect.
                raise TargetCallError("agent-unreachable", "target unreachable")
            self.connected = True
            return {
                "agent_version": "0.9.0-agent",
                "controller_version": "0.9.0-controller",
                "agent_protocol_version": 1,
                "protocol_version": 1,
                "target": "blackbird",
                "daemon_start_ts": "2026-06-03T00:00:00Z",
                "capabilities": ["list_configs", "preview", "gpu", "health"],
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

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target manager should not subscribe")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=DyingAgentClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("t")
        await pilot.pause()
        assert app.screen.id == "target-manager"

        # R from the open manager; the reconnect worker dies on the raised error.
        app.screen.action_reconnect()
        await _wait_for_target_connection_state(app, "unreachable")
        # The still-open modal must flip to the truthful failed state — never a
        # frozen `reconnecting…` contradicting the chrome's unreachable banner.
        await _wait_for_textual_condition(
            pilot,
            lambda: "connection: unreachable"
            in str(app.screen.query_one("#target-manager-detail", Static).content),
            "target manager did not render the failed reconnect state",
        )
        assert app.screen.id == "target-manager"
        detail = str(app.screen.query_one("#target-manager-detail", Static).content)
        assert "reconnecting…" not in detail


@pytest.mark.asyncio
async def test_target_manager_tracks_keepalive_drop_and_recovery_while_open(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-257 companion: the keepalive's state-change branches (drop via
    # _mark_target_disconnected, recovery flip in _target_keepalive_once) also
    # push live state into an open manager, so a mid-session link drop and its
    # automatic recovery render truthfully without the operator pressing R.
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        workdir=Path("/tank/repos/vela"),
        venv=Path("/tank/venvs/vela"),
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

    class FlakyLinkClient:
        def __init__(self) -> None:
            self.connected = False
            self.fail_ping = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "agent_version": "0.9.0-agent",
                "controller_version": "0.9.0-controller",
                "agent_protocol_version": 1,
                "protocol_version": 1,
                "target": "blackbird",
                "daemon_start_ts": "2026-06-03T00:00:00Z",
                "capabilities": ["list_configs", "preview", "gpu", "health"],
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def ping(self):
            if self.fail_ping:
                raise ConnectionError("link down")
            return {"ok": True}

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("keepalive should not subscribe")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    client = FlakyLinkClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=0.05,
        target_ping_timeout_seconds=0.05,
    )

    async with app.run_test() as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("t")
        await pilot.pause()
        assert app.screen.id == "target-manager"
        detail = str(app.screen.query_one("#target-manager-detail", Static).content)
        assert "connection: connected" in detail

        # Mid-session drop: only the keepalive can notice — the open manager
        # must flip to disconnected without any keypress.
        client.fail_ping = True
        await _wait_for_target_connection_state(app, "disconnected")
        await _wait_for_textual_condition(
            pilot,
            lambda: "connection: disconnected"
            in str(app.screen.query_one("#target-manager-detail", Static).content),
            "open manager did not render the keepalive drop",
        )

        # Automatic recovery: the keepalive flip pushes the honest connected
        # card back into the still-open manager.
        client.fail_ping = False
        await _wait_for_target_connection_state(app, "connected")
        await _wait_for_textual_condition(
            pilot,
            lambda: "connection: connected"
            in str(app.screen.query_one("#target-manager-detail", Static).content),
            "open manager did not render the keepalive recovery",
        )
        assert app.screen.id == "target-manager"


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

    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

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
    app = VelaApp(
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
            "ssh_key=/home/bgconley/.ssh/vela_ed25519 "
            "agent_command='/home/bgconley/venvs/current-vela/bin/vela agent connect' "
            "workdir=/tank/repos/vela venv=/tank/venvs/vela"
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
        assert saved.ssh_key == Path("/home/bgconley/.ssh/vela_ed25519")
        assert saved.agent_command == [
            "/home/bgconley/venvs/current-vela/bin/vela",
            "agent",
            "connect",
        ]
        assert saved.workdir == Path("/tank/repos/vela")
        assert saved.venv == Path("/tank/venvs/vela")
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
        ssh_key=Path("/home/bgconley/.ssh/vela_ed25519"),
        agent_command=[
            "/home/bgconley/venvs/current-vela/bin/vela",
            "agent",
            "connect",
        ],
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
    app = VelaApp(
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
        assert "ssh_key=/home/bgconley/.ssh/vela_ed25519" in form.value
        assert (
            "agent_command='/home/bgconley/venvs/current-vela/bin/vela agent connect'"
            in form.value
        )
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
    app = VelaApp(
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
async def test_target_manager_bootstrap_affordance_renders_command(
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
            raise AssertionError("bootstrap affordance should not subscribe")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_client=QuietTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("t")
        await pilot.press("down")
        await pilot.press("b")
        await pilot.pause()

        assert "vela targets bootstrap blackbird" in app.error_text
        assert "--host bgconley@10.25.0.51" in app.error_text
        assert "--install" in app.error_text


@pytest.mark.asyncio
async def test_target_manager_pushes_selected_local_config_to_remote(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
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

    class LocalTargetClient:
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
                            "path": str(config_path),
                            "name": "alpha",
                            "model": "org/alpha",
                            "warnings": [],
                            "config": {"name": "alpha", "model": "org/alpha"},
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {
                    "preview": f"vllm serve {params['name']}",
                    "warnings": [],
                    "metadata": {},
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected local client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("push affordance should not subscribe")

    class RemoteTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            remote_calls.append((method, dict(params or {})))
            if method == "push_config":
                return {
                    "name": params["name"],
                    "path": "/home/bgconley/.config/vela/configs/alpha.yaml",
                    "warnings": [],
                }
            raise AssertionError(f"unexpected remote client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("push config should not subscribe")

    remote_calls: list[tuple[str, dict[str, object]]] = []
    remote_client = RemoteTargetClient()

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda target: remote_client if target.name == "blackbird" else LocalTargetClient(),
        raising=False,
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_client=LocalTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _wait_for_condition(
            lambda: app.current_config is not None
            and app.current_config.name == "alpha",
            "local config was not selected",
        )
        await pilot.press("t")
        await pilot.press("down")
        await pilot.press("p")
        await _wait_for_condition(
            lambda: remote_calls and remote_calls[0][0] == "push_config",
            "push_config was not called",
        )

        params = remote_calls[0][1]
        assert params["name"] == "alpha"
        assert params["yaml"] == "name: alpha\nmodel: org/alpha\n"
        assert "overwrite" not in params
        assert remote_client.connected is False


@pytest.mark.asyncio
async def test_target_manager_push_config_conflict_can_be_cancelled(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
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

    class LocalTargetClient:
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
                            "path": str(config_path),
                            "name": "alpha",
                            "model": "org/alpha",
                            "warnings": [],
                            "config": {"name": "alpha", "model": "org/alpha"},
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": f"vllm serve {params['name']}", "warnings": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected local client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("push conflict test should not subscribe")

    class RemoteTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            remote_calls.append((method, dict(params or {})))
            if method == "push_config":
                raise TargetCallError(
                    "config-exists",
                    "config already exists",
                    {
                        "name": params["name"],
                        "path": "/home/user/.config/vela/configs/alpha.yaml",
                    },
                )
            raise AssertionError(f"unexpected remote client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("push config should not subscribe")

    remote_calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(tui_app_module, "load_targets_file", lambda: FakeTargetsRegistry())
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda target: RemoteTargetClient() if target.name == "blackbird" else LocalTargetClient(),
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_client=LocalTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_condition(
            lambda: app.current_config is not None
            and app.current_config.name == "alpha",
            "local config was not selected",
        )
        await app._push_selected_config_to_target("blackbird")
        await _wait_for_condition(lambda: app.screen.id == "confirm", "confirm did not open")
        await _wait_for_condition(
            lambda: bool(app.screen.query("#confirm-message")),
            "confirm message did not mount",
        )

        message = str(app.screen.query_one("#confirm-message", Static).content)
        assert "Overwrite config alpha on blackbird?" in message
        assert "/home/user/.config/vela/configs/alpha.yaml" in message
        await pilot.press("escape")
        await pilot.pause()

        assert app.screen.id != "confirm"
        assert len(remote_calls) == 1
        assert "overwrite" not in remote_calls[0][1]


@pytest.mark.asyncio
async def test_target_manager_push_config_conflict_confirm_overwrites(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
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

    class LocalTargetClient:
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
                            "path": str(config_path),
                            "name": "alpha",
                            "model": "org/alpha",
                            "warnings": [],
                            "config": {"name": "alpha", "model": "org/alpha"},
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": f"vllm serve {params['name']}", "warnings": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected local client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("push conflict test should not subscribe")

    class RemoteTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            remote_calls.append((method, dict(params or {})))
            if method == "push_config" and not params.get("overwrite"):
                raise TargetCallError(
                    "config-exists",
                    "config already exists",
                    {
                        "name": params["name"],
                        "path": "/home/user/.config/vela/configs/alpha.yaml",
                    },
                )
            if method == "push_config":
                return {
                    "name": params["name"],
                    "path": "/home/user/.config/vela/configs/alpha.yaml",
                    "warnings": [],
                }
            raise AssertionError(f"unexpected remote client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("push config should not subscribe")

    remote_calls: list[tuple[str, dict[str, object]]] = []
    remote_client = RemoteTargetClient()
    monkeypatch.setattr(tui_app_module, "load_targets_file", lambda: FakeTargetsRegistry())
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda target: remote_client if target.name == "blackbird" else LocalTargetClient(),
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_client=LocalTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_condition(
            lambda: app.current_config is not None
            and app.current_config.name == "alpha",
            "local config was not selected",
        )
        await app._push_selected_config_to_target("blackbird")
        await _wait_for_condition(lambda: app.screen.id == "confirm", "confirm did not open")
        await _wait_for_condition(
            lambda: bool(app.screen.query("#confirm-message")),
            "confirm message did not mount",
        )
        await pilot.press("enter")
        await _wait_for_condition(lambda: len(remote_calls) == 2, "overwrite was not called")

        assert "overwrite" not in remote_calls[0][1]
        assert remote_calls[1][1]["overwrite"] is True
        assert remote_calls[1][1]["name"] == "alpha"
        assert remote_calls[1][1]["yaml"] == "name: alpha\nmodel: org/alpha\n"
        assert remote_client.connected is False


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

    app = VelaApp(
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
        assert "vela targets bootstrap local --install" in app.error_text
        assert "(R) Reconnect" in app.error_text
        assert "(t) Switch target" in app.error_text
        assert app.registry.valid == []
        assert app.registry.invalid == []


@pytest.mark.asyncio
async def test_tui_surfaces_agent_auth_required_on_mount(
    config_dir: Path,
) -> None:
    # bug-233: any TargetCallError raised while loading the registry at startup is a
    # connection-surface problem and must never crash the TUI out of on_mount. connect()
    # succeeds here so the failure is isolated to _load_registry_from_agent's except
    # filter; the auth remediation must be rendered by that same handling.
    class AuthRequiredTargetClient:
        connected = False

        async def connect(self):
            self.connected = True
            return None

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                raise TargetCallError(
                    "agent-auth-required",
                    "target agent requires a valid capability token",
                    {"reason": "capability-token-required"},
                )
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            return {}

        def subscribe(self, *_args, **_kwargs):
            raise RuntimeError("gpu stream unavailable in auth-required test")

    app = VelaApp(
        configs_dir=config_dir,
        target_client=AuthRequiredTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        # App reached a mounted state: on_mount ran past _load_registry_from_agent
        # without a traceback escaping run_test(), and the #error banner is mounted.
        assert app.query_one("#error", Static) is not None
        assert app.registry.valid == []
        assert app.registry.invalid == []

        # The agent-auth remediation renders in the banner the same way the
        # version-mismatch / agent-unreachable connection errors do.
        assert "AGENT_AUTH_REQUIRED" in app.error_text
        assert "target agent requires a capability token" in app.error_text
        assert "vela agent gen-token --install --target local" in app.error_text
        assert "(R) Reconnect" in app.error_text
        assert "(t) Switch target" in app.error_text


def test_target_connection_banner_renders_agent_not_installed_remediation(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blackbird = TargetConfig(
        name="blackbird", transport=TransportKind.SSH, host="user@gpu-host"
    )

    class _Registry:
        targets = [TargetConfig(name="local"), blackbird]

        def get(self, name):
            if name == "blackbird":
                return blackbird
            return TargetConfig(name="local")

        def by_name(self, name):
            return self.get(name)

    monkeypatch.setattr(
        tui_app_module, "load_targets_file", lambda: _Registry(), raising=False
    )
    app = VelaApp(configs_dir=config_dir, target_name="blackbird")

    banner = app._render_target_connection_banner(
        "command-not-found",
        "Target agent command not found: vela",
        details={"command": "vela"},
    )

    assert "AGENT_NOT_INSTALLED" in banner
    assert "vela targets bootstrap blackbird --install" in banner


def test_target_connection_banner_renders_ssh_auth_remediation(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blackbird = TargetConfig(
        name="blackbird", transport=TransportKind.SSH, host="user@gpu-host"
    )

    class _Registry:
        targets = [TargetConfig(name="local"), blackbird]

        def get(self, name):
            if name == "blackbird":
                return blackbird
            return TargetConfig(name="local")

        def by_name(self, name):
            return self.get(name)

    monkeypatch.setattr(
        tui_app_module, "load_targets_file", lambda: _Registry(), raising=False
    )
    app = VelaApp(configs_dir=config_dir, target_name="blackbird")

    banner = app._render_target_connection_banner(
        "agent-unreachable",
        "SSH target agent bridge failed",
        details={"reason": "ssh-auth", "stderr": "Permission denied (publickey)."},
    )

    assert "AGENT_UNREACHABLE" in banner
    assert "SSH stderr: Permission denied (publickey)." in banner
    assert "vela targets setup-ssh blackbird" in banner


@pytest.mark.asyncio
async def test_action_load_blocks_when_target_unreachable(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    app = VelaApp(configs_dir=config_dir)
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
    app = VelaApp(
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

    app = VelaApp(
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
    app = VelaApp(
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


@pytest.mark.asyncio
async def test_keepalive_survives_reconnect_and_still_detects_drops(
    config_dir: Path,
) -> None:
    # bug-253: action_reconnect used to spawn its worker in the SAME exclusive
    # "target-connection" group as the keepalive loop, so the first R press
    # CANCELLED keepalive and killed automatic drop detection for the rest of the
    # session. Keepalive now runs in its own group and must survive a reconnect:
    # a drop staged AFTER R is still detected.
    class ReconnectKeepaliveClient:
        def __init__(self) -> None:
            self.connected = False
            self.fail_ping = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def ping(self):
            if self.fail_ping:
                raise ConnectionError("link down")
            return {"ok": True}

        async def call(self, method: str, _params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("keepalive should not subscribe")

    client = ReconnectKeepaliveClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_client=client,
        target_ping_interval_seconds=0.05,
        target_ping_timeout_seconds=0.05,
    )

    async with app.run_test() as pilot:
        await _wait_for_target_connection_state(app, "connected")
        # Press R: under the bug this cancels keepalive (shared exclusive group).
        app.action_reconnect()
        await pilot.pause()
        await _wait_for_target_connection_state(app, "connected")
        # Stage a drop — only a surviving keepalive can notice it.
        client.fail_ping = True
        await _wait_for_target_connection_state(app, "disconnected")


@pytest.mark.asyncio
async def test_keepalive_recovery_transition_reloads_registry(
    config_dir: Path,
) -> None:
    # bug-253: on a non-connected -> connected keepalive flip the loop only
    # refreshed chrome; it must ALSO reload the registry so a config set that
    # changed while the link was down is picked up (a bare chrome refresh would
    # leave the stale offline view frozen). Transition-guarded: only on the flip.
    def _entry(name: str) -> dict[str, object]:
        return {
            "path": f"/agent/configs/{name}.yaml",
            "name": name,
            "model": "org/model",
            "warnings": [],
            "config": {"name": name, "model": "org/model"},
        }

    class RecoveringClient:
        def __init__(self) -> None:
            self.connected = False
            self.fail_ping = False
            self.valid = [_entry("one")]

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def ping(self):
            if self.fail_ping:
                raise ConnectionError("link down")
            return {"ok": True}

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": self.valid, "invalid": []}
            if method == "preview":
                return {"preview": "vllm serve org/model", "warnings": [], "metadata": {}}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("recovery test should not subscribe")

    client = RecoveringClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_client=client,
        target_ping_interval_seconds=0.05,
        target_ping_timeout_seconds=0.05,
    )

    async with app.run_test():
        await _wait_for_target_connection_state(app, "connected")
        await _wait_for_condition(
            lambda: len(app.registry.valid) == 1,
            "registry did not load the initial config set",
        )
        # Link drops; keepalive marks disconnected.
        client.fail_ping = True
        await _wait_for_target_connection_state(app, "disconnected")
        # The agent's config set grows while we were away, then the link recovers.
        client.valid = [_entry("one"), _entry("two")]
        client.fail_ping = False
        await _wait_for_target_connection_state(app, "connected")
        # The recovery flip reloaded the registry (2 now); a bare chrome refresh
        # would have left it at the stale 1.
        await _wait_for_condition(
            lambda: len(app.registry.valid) == 2,
            "recovery transition did not reload the registry",
        )


def test_target_keepalive_uses_exponential_reconnect_backoff(
    config_dir: Path,
) -> None:
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir, target_name="blackbird")

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

    app = VelaApp(
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

    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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

    app = VelaApp(
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

    app = VelaApp(
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
async def test_tui_attached_launch_clears_run_when_exit_event_stream_hangs(
    config_dir: Path, tmp_path: Path
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
                    "returncode": 0,
                    "intentional": True,
                    "phase": Phase.STOPPED.value,
                }
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            await asyncio.Event().wait()
            yield {"event": "unreachable"}

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            assert resume_from == "live"
            return self._events()

    app = VelaApp(
        configs_dir=config_dir,
        target_client=FakeTargetClient(),
    )
    app._target_exit_event_drain_timeout_seconds = 0.01

    async with app.run_test() as pilot:
        await pilot.pause()
        await asyncio.wait_for(app._run_selected_config(), timeout=1)

        assert app.current_run_id is None
        assert app.phase is Phase.STOPPED


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

    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir)
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
    app = VelaApp(configs_dir=config_dir, target_client=target_client)

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
    app = VelaApp(configs_dir=config_dir, target_client=target_client)

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
    app = VelaApp(configs_dir=config_dir, target_client=target_client)

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
    app = VelaApp(configs_dir=config_dir, clock=lambda: 1_000.0)
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
    app = VelaApp(configs_dir=config_dir)
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
    app = VelaApp(configs_dir=config_dir)
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
    app = VelaApp(configs_dir=config_dir, target_name="blackbird")

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

    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir)

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
async def test_help_screen_closes_and_does_not_trap_following_keys(
    config_dir: Path,
) -> None:
    # Regression for bug-221: HelpScreen bound Escape/?/F1 to a bare
    # ``pop_screen`` action that never dismissed the modal, so the modal was a
    # live trap and swallowed the next key (e.g. ``/`` for log search).
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        # Escape closes Help and returns to the dashboard.
        await pilot.press("?")
        await pilot.pause()
        assert app.screen.id == "help"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.id != "help"
        assert len(app.screen_stack) == 1

        # ``?`` toggles Help closed as well.
        await pilot.press("?")
        await pilot.pause()
        assert app.screen.id == "help"
        await pilot.press("?")
        await pilot.pause()
        assert app.screen.id != "help"

        # F1 also closes Help (advertised in the Help body).
        await pilot.press("?")
        await pilot.pause()
        assert app.screen.id == "help"
        await pilot.press("f1")
        await pilot.pause()
        assert app.screen.id != "help"

        # With Help closed, ``/`` is no longer swallowed: it opens log search.
        await pilot.press("/")
        await pilot.pause()
        assert app.screen.id == "log-search-prompt"


@pytest.mark.asyncio
async def test_new_deployment_screen_opens_from_tui_binding(config_dir: Path) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict | None]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {
                    "presets": [
                        {
                            "name": "balanced",
                            "description": "Balanced",
                            "engine": {},
                            "extra_args": [],
                            "applies_to": ["all"],
                        }
                    ]
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment screen should not subscribe")

    client = ComposerClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)

    async with app.run_test() as pilot:
        await pilot.pause()
        palette_titles = {command.title for command in app.get_system_commands(app.screen)}
        assert "New Deployment" in palette_titles
        await pilot.press("n")
        await pilot.pause()

        assert app.screen.id == "new-deployment"
        assert isinstance(app.screen, ModalScreen)
        assert app.screen.query_one("#new-deployment-panel").region.x > 0
        assert ("list_presets", {}) in client.calls
        # Clean walk (A3): the optional-section RPCs returned empty, so no
        # #new-deployment-*-warning row is displayed.
        for _warn_id in (
            "#new-deployment-recipe-warning",
            "#new-deployment-model-warning",
            "#new-deployment-build-warning",
        ):
            assert app.screen.query_one(_warn_id, Static).display is False


@pytest.mark.asyncio
async def test_new_deployment_target_picker_shows_registry_connection_state(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    class ComposerClient:
        connected = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "target": "blackbird",
                "agent_version": "0.9.0",
                "capabilities": ["compose_config", "list_presets"],
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target picker state should not subscribe")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=ComposerClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )

        assert app.screen.query_one("#new-deployment-target-select", Select).value == (
            "blackbird"
        )
        state = str(app.screen.query_one("#new-deployment-target-state", Static).content)
        assert "● blackbird connected" in state
        assert "agent 0.9.0" in state


@pytest.mark.asyncio
async def test_new_deployment_target_picker_probes_non_active_target_dot(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    class ActiveClient:
        connected = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "target": "local",
                "agent_version": "0.9.0-local",
                "capabilities": ["compose_config", "list_presets"],
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected active client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target picker state should not subscribe")

    class ProbeClient:
        def __init__(self, target: TargetConfig) -> None:
            self.target = target
            self.connected = False
            self.connect_calls = 0
            self.disconnect_calls = 0

        async def connect(self) -> dict[str, object]:
            self.connected = True
            self.connect_calls += 1
            return {
                "target": self.target.name,
                "agent_version": "0.9.0-blackbird",
                "capabilities": ["compose_config", "list_presets"],
            }

        async def disconnect(self) -> None:
            self.connected = False
            self.disconnect_calls += 1

        async def call(self, method: str, params):
            raise AssertionError(f"unexpected probe client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target picker probe should not subscribe")

    probe_clients: list[ProbeClient] = []

    def fake_target_client_for_config(target, **_kwargs):
        client = ProbeClient(target)
        probe_clients.append(client)
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

    app = VelaApp(
        configs_dir=config_dir,
        target_client=ActiveClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )

        option_labels = [label for label, _value in app.screen._target_options()]
        assert any(label.startswith("● blackbird") for label in option_labels)

    assert [client.target.name for client in probe_clients] == ["blackbird"]
    assert probe_clients[0].connect_calls == 1
    assert probe_clients[0].disconnect_calls == 1


@pytest.mark.asyncio
async def test_new_deployment_target_picker_switches_target_before_composing(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    class ComposerTargetClient:
        def __init__(self, target: TargetConfig) -> None:
            self.target = target
            self.connected = False
            self.disconnect_calls = 0
            self.compose_params: dict | None = None
            self.calls: list[tuple[str, dict | None]] = []

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "target": self.target.name,
                "agent_version": f"0.9.0-{self.target.name}",
                "capabilities": [
                    "compose_config",
                    "list_deployment_recipes",
                    "list_models",
                    "list_builds",
                    "list_presets",
                ],
            }

        async def disconnect(self) -> None:
            self.connected = False
            self.disconnect_calls += 1

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                assert params == {"target": self.target.name}
                return {"recipes": []}
            if method == "list_models":
                return {"models": []}
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen-targeted",
                        "target": self.target.name,
                        "model": "Qwen/Qwen3-32B",
                        "command": {"runtime": "process"},
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {
                    "preview": f"cwd=/{self.target.name}\nvllm serve Qwen/Qwen3-32B",
                    "warnings": [],
                    "metadata": {},
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment target picker should not subscribe")

    target_clients: list[ComposerTargetClient] = []

    def fake_target_client_for_config(target, **_kwargs):
        client = ComposerTargetClient(target)
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

    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen-targeted"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#new-deployment-target-select", Select).value = "blackbird"

        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.target_name == "blackbird"
            and app.screen.query_one("#new-deployment-target-select", Select).value
            == "blackbird",
            "new deployment target picker did not switch to blackbird",
        )
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "new deployment review did not open after target picker switch",
        )

    local_client = next(
        client for client in target_clients if client.target.name == "local" and client.calls
    )
    blackbird_client = next(
        client
        for client in target_clients
        if client.target.name == "blackbird" and client.compose_params is not None
    )
    assert local_client.disconnect_calls == 1
    assert blackbird_client.compose_params is not None
    assert blackbird_client.compose_params["target"] == "blackbird"
    assert blackbird_client.compose_params["name"] == "qwen-targeted"
    assert blackbird_client.compose_params["model"] == "Qwen/Qwen3-32B"
    assert local_client.compose_params is None


@pytest.mark.asyncio
async def test_new_deployment_wizard_steps_forward_and_back_preserve_edits(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {
                    "presets": [
                        {
                            "name": "balanced",
                            "description": "Balanced",
                            "engine": {},
                            "extra_args": [],
                            "applies_to": ["all"],
                        }
                    ]
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment wizard should not subscribe")

    app = VelaApp(configs_dir=config_dir, target_client=ComposerClient())

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

        assert "Target" in str(
            app.screen.query_one("#new-deployment-current-step", Static).content
        )

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert "Runtime" in str(
            app.screen.query_one("#new-deployment-current-step", Static).content
        )
        app.screen.query_one("#new-deployment-runtime", Select).value = "docker"

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert "Model" in str(
            app.screen.query_one("#new-deployment-current-step", Static).content
        )
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert "Customize" in str(
            app.screen.query_one("#new-deployment-current-step", Static).content
        )
        app.screen.query_one("#new-deployment-port", Input).value = "18001"

        await pilot.press("ctrl+b")
        await pilot.pause()
        assert "Model" in str(
            app.screen.query_one("#new-deployment-current-step", Static).content
        )
        assert app.screen.query_one("#new-deployment-model", Input).value == "Qwen/Qwen3-32B"

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert "Customize" in str(
            app.screen.query_one("#new-deployment-current-step", Static).content
        )
        assert app.screen.query_one("#new-deployment-port", Input).value == "18001"


@pytest.mark.parametrize(
    ("runtime_value", "field_id", "field_value", "expected_runtime"),
    [
        (
            "build",
            "#new-deployment-build",
            "vllm-nightly-cu130-sm120",
            {"kind": "build", "build": "vllm-nightly-cu130-sm120"},
        ),
        (
            "executable",
            "#new-deployment-executable",
            "/opt/vllm/bin/vllm",
            {"kind": "executable", "executable": "/opt/vllm/bin/vllm"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_new_deployment_runtime_picker_hands_build_and_executable_to_composer(
    config_dir: Path,
    runtime_value: str,
    field_id: str,
    field_value: str,
    expected_runtime: dict[str, str],
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.compose_params: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen3-runtime",
                        "target": "local",
                        "model": "Qwen/Qwen3-32B",
                        "command": {"runtime": "process"},
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve Qwen/Qwen3-32B", "warnings": []}
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment runtime picker should not subscribe")

    client = ComposerClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#new-deployment-name", Input).value = "qwen3-runtime"
        app.screen.query_one("#new-deployment-runtime", Select).value = runtime_value
        app.screen.query_one(field_id, Input).value = field_value
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "new deployment review did not open for runtime picker selection",
        )

    assert client.compose_params is not None
    assert client.compose_params["runtime"] == expected_runtime


@pytest.mark.asyncio
async def test_new_deployment_recipe_selection_prefills_blackbird_runtime(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.compose_params: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {
                    "presets": [
                        {
                            "name": "balanced",
                            "description": "Balanced",
                            "engine": {},
                            "extra_args": [],
                            "applies_to": ["all"],
                        }
                    ]
                }
            if method == "list_deployment_recipes":
                assert params == {"target": "blackbird"}
                return {
                    "recipes": [
                        {
                            "key": "blackbird-qwen36-27b-fp8-rp6000",
                            "label": "Blackbird Qwen3.6 27B FP8 RP6000",
                            "target": "blackbird",
                            "runtime": "docker",
                            "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
                            "model": "Qwen/Qwen3.6-27B-FP8",
                            "served_model_name": "qwen36-27b-fp8-kvfp8-rp6000",
                            "image": "vllm/vllm-openai@sha256:b13d",
                            "server": {
                                "host": "0.0.0.0",
                                "port": 18003,
                                "exposure": "lan",
                            },
                        }
                    ]
                }
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
                        "target": "blackbird",
                        "model": "Qwen/Qwen3.6-27B-FP8",
                        "command": {
                            "runtime": "docker",
                            "docker": {
                                "image": "vllm/vllm-openai@sha256:b13d",
                                "container_name": (
                                    "vela-qwen36-27b-fp8-kvfp8-rp6000-blackbird"
                                ),
                            },
                        },
                        "server": {
                            "host": "0.0.0.0",
                            "port": 18003,
                            "exposure": "lan",
                        },
                    },
                    "warnings": [],
                    "derived": [
                        {
                            "field": "deployment.recipe",
                            "value": "blackbird-qwen36-27b-fp8-rp6000",
                            "source": "lab_recipe",
                        }
                    ],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {
                    "preview": (
                        "docker run ... vllm/vllm-openai@sha256:b13d "
                        "--attention-backend FLASHINFER"
                    ),
                    "warnings": [],
                    "metadata": {},
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment recipe selection should not subscribe")

    client = ComposerClient()
    app = VelaApp(configs_dir=config_dir, target_name="blackbird", target_client=client)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

        app.screen.query_one("#new-deployment-recipe", Select).value = (
            "blackbird-qwen36-27b-fp8-rp6000"
        )
        await pilot.pause()

        assert (
            app.screen.query_one("#new-deployment-name", Input).value
            == "qwen36-27b-fp8-kvfp8-rp6000-blackbird"
        )
        assert app.screen.query_one("#new-deployment-runtime", Select).value == "docker"
        assert (
            app.screen.query_one("#new-deployment-model", Input).value
            == "Qwen/Qwen3.6-27B-FP8"
        )
        assert (
            app.screen.query_one("#new-deployment-image", Input).value
            == "vllm/vllm-openai@sha256:b13d"
        )
        assert app.screen.query_one("#new-deployment-host", Input).value == "0.0.0.0"
        assert app.screen.query_one("#new-deployment-port", Input).value == "18003"
        assert app.screen.query_one("#new-deployment-exposure", Select).value == "lan"

        # Clean walk (A3): recipes/models/builds returned data or empty, so no
        # #new-deployment-*-warning row is displayed.
        for _warn_id in (
            "#new-deployment-recipe-warning",
            "#new-deployment-model-warning",
            "#new-deployment-build-warning",
        ):
            assert app.screen.query_one(_warn_id, Static).display is False

        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()

        assert client.compose_params is not None
        assert client.compose_params["target"] == "blackbird"
        assert client.compose_params["model"] == "Qwen/Qwen3.6-27B-FP8"
        assert client.compose_params["runtime"] == {
            "kind": "docker",
            "image": "vllm/vllm-openai@sha256:b13d",
        }
        assert client.compose_params["overrides"]["server"] == {
            "host": "0.0.0.0",
            "exposure": "lan",
            "port": 18003,
        }
        assert app.screen.id == "new-deployment-review"
        assert "deployment.recipe" in str(
            app.screen.query_one("#new-deployment-review-derived", Static).content
        )


@pytest.mark.asyncio
async def test_new_deployment_selects_target_model_and_build_from_tui(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.compose_params: dict | None = None
            self.calls: list[str] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append(method)
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_builds":
                return {
                    "builds": [
                        {
                            "build_id": "01NIGHTLY",
                            "label": "nightly-cu130",
                            "status": "ready",
                            "resolved": {"vllm": "0.20.2rc1.dev9", "cuda": "13.0"},
                        }
                    ],
                    "skipped": [],
                }
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "qwen-fp8-pin",
                            "display_name": "Qwen3.6 27B FP8",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3.6-27B-FP8",
                            "commit_sha": "abc123",
                            "cache_state": "cached",
                        }
                    ]
                }
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen-pinned",
                        "target": "blackbird",
                        "model": "Qwen/Qwen3.6-27B-FP8",
                        "model_ref": "qwen-fp8-pin",
                        "revision": "abc123",
                        "command": {"runtime": "process", "build": "nightly-cu130"},
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nnightly-cu130/bin/vllm serve Qwen/Qwen3.6-27B-FP8",
                    "warnings": [],
                    "metadata": {},
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment selector test should not subscribe")

    client = ComposerClient()
    app = VelaApp(configs_dir=config_dir, target_name="blackbird", target_client=client)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()

        app.screen.query_one("#new-deployment-name", Input).value = "qwen-pinned"
        app.screen.query_one("#new-deployment-model-ref", Select).value = "qwen-fp8-pin"
        await pilot.pause()
        app.screen.query_one("#new-deployment-build-select", Select).value = "nightly-cu130"
        await pilot.pause()

        assert (
            app.screen.query_one("#new-deployment-model", Input).value
            == "Qwen/Qwen3.6-27B-FP8"
        )
        assert app.screen.query_one("#new-deployment-runtime", Select).value == "build"
        assert app.screen.query_one("#new-deployment-build", Input).value == "nightly-cu130"

        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "new deployment review did not open for target model/build selectors",
        )

    assert client.compose_params is not None
    assert client.compose_params["model_ref"] == "qwen-fp8-pin"
    assert client.compose_params["revision"] == "abc123"
    assert client.compose_params["runtime"] == {"kind": "build", "build": "nightly-cu130"}
    assert "list_models" in client.calls
    assert "list_builds" in client.calls


@pytest.mark.asyncio
async def test_new_deployment_create_build_handoff_pins_created_build(
    config_dir: Path,
) -> None:
    class FakeEvents:
        def __init__(self) -> None:
            self.closed = False
            self._events: list[dict[str, object]] = []

        def arm(self, job_id: str, label: str) -> None:
            self._events = [
                {
                    "event": "job_progress",
                    "job_id": job_id,
                    "kind": "committed",
                    "text": "Creating build",
                    "level": "INFO",
                },
                {
                    "event": "job_done",
                    "job_id": job_id,
                    "ok": True,
                    "detail": "build ready",
                    "label": label,
                    "build_id": "01CREATED",
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

    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.events = FakeEvents()
            self.create_calls: list[dict[str, object]] = []
            self.compose_params: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_models":
                return {"models": []}
            if method == "list_builds":
                builds = []
                if self.create_calls:
                    builds.append(
                        {
                            "build_id": "01CREATED",
                            "label": "nightly-cu130-sm120",
                            "status": "ready",
                            "resolved": {"vllm": "0.20.2rc1.dev9", "cuda": "13.0"},
                        }
                    )
                return {"builds": builds, "skipped": []}
            if method == "check_build_prerequisites":
                return {"ok": True, "method": params["method"], "uv_available": True}
            if method == "create_build":
                self.create_calls.append(dict(params))
                self.events.arm(str(params["job_id"]), str(params["label"]))
                return {
                    "job_id": params["job_id"],
                    "kind": "create_build",
                    "status": "running",
                }
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen-created",
                        "target": "blackbird",
                        "model": "Qwen/Qwen3.6-27B-FP8",
                        "command": {
                            "runtime": "process",
                            "build": "nightly-cu130-sm120",
                        },
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {
                    "preview": (
                        "cwd=/agent\nnightly-cu130-sm120/bin/vllm serve "
                        "Qwen/Qwen3.6-27B-FP8"
                    ),
                    "warnings": [],
                    "metadata": {},
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            if list(run_ids) == ["__agent__"]:
                async def gpu_events():
                    while True:
                        await asyncio.sleep(60)
                        yield {}

                return gpu_events()
            return self.events

    client = ComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and bool(app.screen.query("#new-deployment-runtime SelectCurrent #label")),
            "new deployment screen did not open with composed selects",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen-created"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3.6-27B-FP8"
        app.screen.query_one("#new-deployment-runtime", Select).value = "create_build"

        await _wait_for_condition(
            lambda: app.screen.id == "create-build"
            and bool(app.screen.query("#create-build-method SelectCurrent #label")),
            "create build handoff did not open the build flow",
        )
        app.screen.query_one("#create-build-method", Select).value = "nightly"
        app.screen.query_one("#create-build-label", Input).value = "nightly-cu130-sm120"
        app.screen.query_one("#create-build-channel", Input).value = "cu130"
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-runtime", Select).value == "build"
            and app.screen.query_one("#new-deployment-build", Input).value
            == "nightly-cu130-sm120"
            and "Review"
            in str(app.screen.query_one("#new-deployment-current-step", Static).content),
            "created build was not returned to the new deployment wizard",
        )
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "new deployment review did not open after create-build handoff",
        )

    assert client.create_calls
    assert client.compose_params is not None
    assert client.compose_params["runtime"] == {
        "kind": "build",
        "build": "nightly-cu130-sm120",
    }


@pytest.mark.asyncio
async def test_new_deployment_adopt_venv_handoff_pins_adopted_build(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.adopt_calls: list[dict[str, object]] = []
            self.compose_params: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_models":
                return {"models": []}
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method == "adopt_build":
                self.adopt_calls.append(dict(params))
                return {
                    "build_id": "01ADOPTED",
                    "label": "external-nightly",
                    "status": "adopted",
                }
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen-adopted",
                        "target": "blackbird",
                        "model": "Qwen/Qwen3.6-27B-FP8",
                        "command": {"runtime": "process", "build": "external-nightly"},
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {
                    "preview": (
                        "cwd=/agent\nexternal-nightly/bin/vllm serve "
                        "Qwen/Qwen3.6-27B-FP8"
                    ),
                    "warnings": [],
                    "metadata": {},
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("adopt-venv new deployment flow should not subscribe")

    client = ComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            # Screen id registers before children mount on slow hosts
            # (bug-207/209/248): gate on a child too, not the id alone.
            lambda: app.screen.id == "new-deployment"
            and bool(app.screen.query("#new-deployment-name")),
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen-adopted"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3.6-27B-FP8"
        app.screen.query_one("#new-deployment-runtime", Select).value = "adopt_build"

        await _wait_for_condition(
            lambda: app.screen.id == "adopt-build"
            and bool(app.screen.query("#adopt-build-label")),
            "adopt venv handoff did not open the adopt build flow",
        )
        app.screen.query_one("#adopt-build-label", Input).value = "external-nightly"
        app.screen.query_one("#adopt-build-venv-path", Input).value = (
            "/agent/venvs/vllm-nightly"
        )
        app.screen.query_one("#adopt-build-vllm-version", Input).value = "0.20.2rc1.dev9"
        app.screen.query_one("#adopt-build-vllm-version-profile", Input).value = "0.11"
        await pilot.press("enter")

        # query_one inside the condition can raise NoMatches during the reopen's
        # mount gap; the textual variant retries instead of propagating it
        # (bare _wait_for_condition would fail the test on the first raise).
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-runtime", Select).value == "build"
            and app.screen.query_one("#new-deployment-build", Input).value
            == "external-nightly"
            and "Review"
            in str(app.screen.query_one("#new-deployment-current-step", Static).content),
            "adopted build was not returned to the new deployment wizard",
        )
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "new deployment review did not open after adopt-venv handoff",
        )

    assert client.adopt_calls == [
        {
            "label": "external-nightly",
            "venv_path": "/agent/venvs/vllm-nightly",
            "vllm_version": "0.20.2rc1.dev9",
            "vllm_version_profile": "0.11",
        }
    ]
    assert client.compose_params is not None
    assert client.compose_params["runtime"] == {
        "kind": "build",
        "build": "external-nightly",
    }


@pytest.mark.asyncio
async def test_new_deployment_pin_hf_repo_handoff_downloads_and_pins_model_ref(
    config_dir: Path,
) -> None:
    class FakeEvents:
        def __init__(self) -> None:
            self.closed = False
            self._events: list[dict[str, object]] = []

        def arm(self, job_id: str) -> None:
            self._events = [
                {
                    "event": "job_progress",
                    "job_id": job_id,
                    "kind": "committed",
                    "text": "Downloading model",
                    "level": "INFO",
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

    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.events = FakeEvents()
            self.pin_calls: list[dict[str, object]] = []
            self.download_calls: list[dict[str, object]] = []
            self.compose_params: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method == "list_models":
                models = []
                if self.pin_calls:
                    models.append(
                        {
                            "entry_id": "qwen-fp8-pin",
                            "display_name": "Qwen3.6 27B FP8",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3.6-27B-FP8",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "cache_state": "remote_only",
                            "gated": True,
                            "token_required": True,
                        }
                    )
                return {"models": models}
            if method == "pin_model":
                self.pin_calls.append(dict(params))
                return {
                    "entry": {
                        "entry_id": "qwen-fp8-pin",
                        "display_name": "Qwen3.6 27B FP8",
                        "source": "hf_repo",
                        "repo_id": "Qwen/Qwen3.6-27B-FP8",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "cache_state": "remote_only",
                        "gated": True,
                        "token_required": True,
                    },
                    "warnings": [
                        {
                            "kind": "remote-only-unresolved",
                            "detail": (
                                "pinned remote-only model has no immutable commit sha; "
                                "launch will be blocked until it is re-pinned online"
                            ),
                        }
                    ],
                }
            if method == "download_model":
                self.download_calls.append(dict(params))
                self.events.arm(str(params["job_id"]))
                return {
                    "job_id": params["job_id"],
                    "kind": "download_model",
                    "status": "running",
                }
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen-pinned",
                        "target": "blackbird",
                        "model": "Qwen/Qwen3.6-27B-FP8",
                        "model_ref": "qwen-fp8-pin",
                        "revision": "abc123",
                        "command": {"runtime": "process"},
                    },
                    "warnings": ["gated-needs-token"],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve Qwen/Qwen3.6-27B-FP8",
                    "warnings": [],
                    "metadata": {},
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            if list(run_ids) == ["__agent__"]:
                async def gpu_events():
                    while True:
                        await asyncio.sleep(60)
                        yield {}

                return gpu_events()
            return self.events

    client = ComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen-pinned"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3.6-27B-FP8"
        app.screen.query_one("#new-deployment-model-revision", Input).value = "main"
        app.screen.query_one("#new-deployment-download-now", Checkbox).value = True
        app.screen.query_one("#new-deployment-model-mode", Select).value = "pin_hf"

        await _wait_for_condition(
            lambda: app.screen.id == "pin-model" and bool(app.screen.query(Input)),
            "pin-HF handoff did not open the pin model flow",
        )
        assert (
            app.screen.query_one("#pin-model-repo-id", Input).value
            == "Qwen/Qwen3.6-27B-FP8"
        )
        assert app.screen.query_one("#pin-model-revision", Input).value == "main"
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-model-ref", Select).value
            == "qwen-fp8-pin"
            and "Review"
            in str(app.screen.query_one("#new-deployment-current-step", Static).content),
            "pinned model was not returned to the new deployment wizard",
        )
        state = str(app.screen.query_one("#new-deployment-model-state", Static).content)
        assert "cache: remote_only" in state
        assert "auth: gated, requires HF_TOKEN" in state
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review" and client.events.closed,
            "new deployment review did not open after download-now model handoff",
        )
        warnings = str(
            app.screen.query_one("#new-deployment-review-warnings", Static).content
        )
        assert "pinned remote-only model has no immutable commit sha" in warnings

    assert client.pin_calls == [
        {
            "repo_id": "Qwen/Qwen3.6-27B-FP8",
            "revision": "main",
        }
    ]
    assert len(client.download_calls) == 1
    assert client.download_calls[0]["model_ref"] == "qwen-fp8-pin"
    assert client.download_calls[0]["revision"] == "abc123"
    assert client.compose_params is not None
    assert client.compose_params["model_ref"] == "qwen-fp8-pin"
    assert client.compose_params["revision"] == "abc123"
    assert "download_now" not in client.compose_params


@pytest.mark.asyncio
async def test_new_deployment_review_blocks_download_now_without_pin(
    config_dir: Path,
) -> None:
    # bug-236 regression pin for the review-time gate the wizard-side fix must
    # NOT remove: source=Existing pin + Download-now checked + pinned Select
    # left at its no-pins placeholder (no pin selected) → _collect_spec emits
    # download_now=True with no model_ref → _review_new_deployment must block
    # before composing and reopen the wizard with the pinned-model error.
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.compose_params: dict | None = None
            self.download_calls: list[dict[str, object]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method == "list_models":
                return {"models": []}
            if method == "suggest_deployment_defaults":
                return {"engine_suggestions": {}, "warnings": [], "sources": []}
            if method == "download_model":
                self.download_calls.append(dict(params))
                return {
                    "job_id": params.get("job_id"),
                    "kind": "download_model",
                    "status": "running",
                }
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen-nopin",
                        "target": "blackbird",
                        "model": "Qwen/Qwen3-32B",
                        "command": {"runtime": "process"},
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "vllm serve Qwen/Qwen3-32B", "warnings": [], "metadata": {}}
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("the pinned-model gate must fire before any job subscription")

    client = ComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            # Screen id registers before children mount on slow hosts
            # (bug-207/209/248): gate on a child too, not the id alone.
            lambda: app.screen.id == "new-deployment"
            and bool(app.screen.query("#new-deployment-name")),
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen-nopin"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        # The empty registry now defaults the source to "Bare repo id" (bug-236b),
        # so drive the trap explicitly: switch to "Existing pin" ANYWAY and leave
        # the pinned Select at its no-pins placeholder — no pin is actually
        # selected (its value stays the __custom__ no-op sentinel).
        app.screen.query_one("#new-deployment-model-mode", Select).value = "existing"
        await pilot.pause()
        assert (
            app.screen.query_one("#new-deployment-model-mode", Select).value == "existing"
        )
        assert (
            app.screen.query_one("#new-deployment-model-ref", Select).value == "__custom__"
        )
        app.screen.query_one("#new-deployment-download-now", Checkbox).value = True
        await pilot.press("ctrl+s")

        # query_one inside the condition can raise NoMatches during the reopen's
        # mount gap; the textual variant retries instead of propagating it
        # (bare _wait_for_condition would fail the test on the first raise).
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "new-deployment"
            and "Download now requires a pinned model"
            in str(app.screen.query_one("#new-deployment-error", Static).content),
            "review did not reopen the wizard with the pinned-model gate error",
        )
        error_text = str(app.screen.query_one("#new-deployment-error", Static).content)
        assert "Pin the HF repo or choose an existing pin" in error_text
        # The draft round-trips: the box stays visible AND checked for the
        # pinnable source — the gate blocks, it does not reset the operator.
        download = app.screen.query_one("#new-deployment-download-now", Checkbox)
        assert download.display is True
        assert download.value is True

    # The gate fired before any agent work: no download job, no compose.
    assert client.download_calls == []
    assert client.compose_params is None


@pytest.mark.asyncio
async def test_new_deployment_build_pin_and_smoke_acceptance_flow(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
    )

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [blackbird]

        def by_name(self, name: str) -> TargetConfig:
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    class EventStream:
        def __init__(self, events: list[dict[str, object]] | None = None) -> None:
            self.events = list(events or [])
            self.closed = False

        def arm(self, events: list[dict[str, object]]) -> None:
            self.events = list(events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.events:
                raise StopAsyncIteration
            return self.events.pop(0)

        async def aclose(self) -> None:
            self.closed = True

    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.create_calls: list[dict[str, object]] = []
            self.pin_calls: list[dict[str, object]] = []
            self.download_calls: list[dict[str, object]] = []
            self.saved_config: dict | None = None
            self.compose_params: dict | None = None
            self.calls: list[tuple[str, dict | None]] = []
            self.job_events: dict[str, EventStream] = {}
            self.smoke_events = EventStream(
                [
                    {
                        "event": "exited",
                        "run_id": "acceptance-smoke-run",
                        "returncode": 0,
                        "intentional": False,
                        "phase": Phase.STOPPED.value,
                        "seq": 1,
                        "ts": "2026-06-07T00:00:00Z",
                        "mono": 1.0,
                    }
                ]
            )

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "target": "blackbird",
                "agent_version": "0.9.0",
                "capabilities": [
                    "compose_config",
                    "list_deployment_recipes",
                    "list_models",
                    "list_builds",
                    "list_presets",
                ],
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                if self.saved_config is None:
                    return {"valid": [], "invalid": []}
                return {
                    "valid": [
                        {
                            "path": str(config_dir / "qwen-acceptance.yaml"),
                            "name": "qwen-acceptance",
                            "model": "Qwen/Qwen3.6-27B-FP8",
                            "target": "blackbird",
                            "warnings": [],
                            "config": self.saved_config,
                        }
                    ],
                    "invalid": [],
                }
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_builds":
                builds = []
                if self.create_calls:
                    builds.append(
                        {
                            "build_id": "01CREATED",
                            "label": "nightly-cu130-sm120",
                            "status": "ready",
                        }
                    )
                return {"builds": builds, "skipped": []}
            if method == "list_models":
                models = []
                if self.pin_calls:
                    models.append(
                        {
                            "entry_id": "qwen-fp8-pin",
                            "display_name": "Qwen3.6 27B FP8",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3.6-27B-FP8",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "cache_state": "remote_only",
                            "gated": True,
                            "token_required": True,
                        }
                    )
                return {"models": models}
            if method == "check_build_prerequisites":
                return {"ok": True, "method": params["method"], "uv_available": True}
            if method == "create_build":
                self.create_calls.append(dict(params))
                job_id = str(params["job_id"])
                self.job_events.setdefault(job_id, EventStream()).arm(
                    [
                        {
                            "event": "job_progress",
                            "job_id": job_id,
                            "kind": "committed",
                            "text": "Creating build",
                            "level": "INFO",
                        },
                        {
                            "event": "job_done",
                            "job_id": job_id,
                            "ok": True,
                            "detail": "build ready",
                            "label": "nightly-cu130-sm120",
                            "build_id": "01CREATED",
                        },
                    ]
                )
                return {"job_id": job_id, "kind": "create_build", "status": "running"}
            if method == "pin_model":
                self.pin_calls.append(dict(params))
                return {
                    "entry": {
                        "entry_id": "qwen-fp8-pin",
                        "display_name": "Qwen3.6 27B FP8",
                        "source": "hf_repo",
                        "repo_id": "Qwen/Qwen3.6-27B-FP8",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "cache_state": "remote_only",
                        "gated": True,
                        "token_required": True,
                    }
                }
            if method == "download_model":
                self.download_calls.append(dict(params))
                job_id = str(params["job_id"])
                self.job_events.setdefault(job_id, EventStream()).arm(
                    [
                        {
                            "event": "job_progress",
                            "job_id": job_id,
                            "kind": "committed",
                            "text": "Downloading model",
                            "level": "INFO",
                        },
                        {
                            "event": "job_done",
                            "job_id": job_id,
                            "ok": True,
                            "detail": "model cached",
                        },
                    ]
                )
                return {"job_id": job_id, "kind": "download_model", "status": "running"}
            if method == "compose_config":
                self.compose_params = dict(params)
                assert params["runtime"] == {
                    "kind": "build",
                    "build": "nightly-cu130-sm120",
                }
                assert params["model_ref"] == "qwen-fp8-pin"
                assert params["revision"] == "abc123"
                return {
                    "config": {
                        "name": "qwen-acceptance",
                        "target": "blackbird",
                        "model": "Qwen/Qwen3.6-27B-FP8",
                        "model_ref": "qwen-fp8-pin",
                        "revision": "abc123",
                        "server": {"host": "127.0.0.1", "port": 18003},
                        "command": {
                            "runtime": "process",
                            "build": "nightly-cu130-sm120",
                        },
                    },
                    "warnings": ["gated-needs-token"],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {
                    "preview": (
                        "cwd=/agent\nnightly-cu130-sm120/bin/vllm serve "
                        "Qwen/Qwen3.6-27B-FP8"
                    ),
                    "warnings": [],
                    "metadata": {},
                }
            if method == "preflight":
                return {"ok": True, "failures": []}
            if method == "save_config":
                self.saved_config = dict(params["config"])
                return {
                    "path": str(config_dir / "qwen-acceptance.yaml"),
                    "name": "qwen-acceptance",
                    "config": self.saved_config,
                }
            if method == "prepare_launch":
                assert params["name"] == "qwen-acceptance"
                return {
                    "config": self.saved_config,
                    "build": {
                        "argv": [
                            "nightly-cu130-sm120/bin/vllm",
                            "serve",
                            "Qwen/Qwen3.6-27B-FP8",
                        ],
                        "env": {},
                        "cwd": str(config_dir),
                        "warnings": [],
                        "metadata": {"vllm_version_profile": "current"},
                        "preview": (
                            "cwd=/agent\nnightly-cu130-sm120/bin/vllm serve "
                            "Qwen/Qwen3.6-27B-FP8"
                        ),
                    },
                    "preflight": None,
                }
            if method == "launch":
                assert params["name"] == "qwen-acceptance"
                return {
                    "run_id": "acceptance-smoke-run",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "probe_until_ready":
                assert params["run_id"] == "acceptance-smoke-run"
                return {
                    "run_id": "acceptance-smoke-run",
                    "ready": True,
                    "detail": "ready",
                    "models": ["qwen-acceptance"],
                    "reachable_url": "http://127.0.0.1:18003",
                    "error_kind": None,
                }
            if method == "stop":
                assert params["run_id"] == "acceptance-smoke-run"
                return {"run_id": "acceptance-smoke-run", "status": "stopped"}
            if method == "wait":
                assert params["run_id"] == "acceptance-smoke-run"
                return {
                    "run_id": "acceptance-smoke-run",
                    "returncode": 0,
                    "intentional": False,
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            if list(run_ids) == ["__agent__"]:
                async def gpu_events():
                    while True:
                        await asyncio.sleep(60)
                        yield {}

                return gpu_events()
            if list(run_ids) == ["acceptance-smoke-run"]:
                return self.smoke_events
            job_id = str(list(run_ids)[0])
            return self.job_events.setdefault(job_id, EventStream())

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )

    client = ComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and bool(app.screen.query("#new-deployment-runtime SelectCurrent #label")),
            "new deployment screen did not open with composed selects",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen-acceptance"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3.6-27B-FP8"
        app.screen.query_one("#new-deployment-model-revision", Input).value = "main"
        app.screen.query_one("#new-deployment-runtime", Select).value = "create_build"

        await _wait_for_condition(
            lambda: app.screen.id == "create-build"
            and bool(app.screen.query("#create-build-method SelectCurrent #label")),
            "acceptance flow did not open create-build",
        )
        app.screen.query_one("#create-build-method", Select).value = "nightly"
        app.screen.query_one("#create-build-label", Input).value = "nightly-cu130-sm120"
        app.screen.query_one("#create-build-channel", Input).value = "cu130"
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-runtime", Select).value == "build"
            and app.screen.query_one("#new-deployment-build", Input).value
            == "nightly-cu130-sm120",
            "acceptance flow did not return created build",
        )
        app.screen.query_one("#new-deployment-model-mode", Select).value = "pin_hf"

        await _wait_for_condition(
            lambda: app.screen.id == "pin-model" and bool(app.screen.query(Input)),
            "acceptance flow did not open pin-model",
        )
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-model-ref", Select).value
            == "qwen-fp8-pin",
            "acceptance flow did not return pinned model",
        )
        # Download-now is a pinnable-source affordance (bug-236a); the empty
        # registry now opens on the bare source (bug-236b), so check it here —
        # once the model is pinned and the box is actually visible.
        app.screen.query_one("#new-deployment-download-now", Checkbox).value = True
        await pilot.press("ctrl+s")

        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "acceptance flow did not open review",
        )
        await pilot.press("s")
        await _wait_for_condition(
            lambda: app.phase is Phase.STOPPED,
            "acceptance flow did not smoke to stopped",
        )

    assert client.create_calls
    assert client.pin_calls == [
        {"repo_id": "Qwen/Qwen3.6-27B-FP8", "revision": "main"}
    ]
    assert len(client.download_calls) == 1
    assert client.download_calls[0]["model_ref"] == "qwen-fp8-pin"
    assert client.saved_config is not None
    assert client.saved_config["command"]["build"] == "nightly-cu130-sm120"
    assert client.compose_params is not None
    methods = [method for method, _params in client.calls]
    assert methods.index("save_config") < methods.index("prepare_launch")
    assert methods.index("prepare_launch") < methods.index("launch")
    assert methods.index("launch") < methods.index("probe_until_ready")
    assert methods.index("probe_until_ready") < methods.index("stop")


@pytest.mark.asyncio
async def test_new_deployment_adopt_local_model_path_handoff_pins_model_ref(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.pin_calls: list[dict[str, object]] = []
            self.compose_params: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method == "list_models":
                models = []
                if self.pin_calls:
                    models.append(
                        {
                            "entry_id": "local-qwen",
                            "display_name": "Local Qwen",
                            "source": "local_path",
                            "local_path": "/agent/models/qwen",
                            "cache_state": "local",
                        }
                    )
                return {"models": models}
            if method == "pin_model":
                self.pin_calls.append(dict(params))
                return {
                    "entry": {
                        "entry_id": "local-qwen",
                        "display_name": "Local Qwen",
                        "source": "local_path",
                        "local_path": "/agent/models/qwen",
                        "cache_state": "local",
                    }
                }
            if method == "compose_config":
                self.compose_params = dict(params)
                return {
                    "config": {
                        "name": "qwen-local",
                        "target": "blackbird",
                        "model": "/agent/models/qwen",
                        "model_ref": "local-qwen",
                        "command": {"runtime": "process"},
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve /agent/models/qwen",
                    "warnings": [],
                    "metadata": {},
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("adopt-local new deployment flow should not subscribe")

    client = ComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen-local"
        app.screen.query_one("#new-deployment-model", Input).value = "/agent/models/qwen"
        app.screen.query_one("#new-deployment-model-mode", Select).value = "adopt_local"

        await _wait_for_condition(
            lambda: app.screen.id == "pin-model" and bool(app.screen.query(Input)),
            "adopt-local handoff did not open the pin model flow",
        )
        assert (
            app.screen.query_one("#pin-model-local-path", Input).value
            == "/agent/models/qwen"
        )
        await pilot.press("enter")

        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-model-ref", Select).value
            == "local-qwen"
            and "Review"
            in str(app.screen.query_one("#new-deployment-current-step", Static).content),
            "local model was not returned to the new deployment wizard",
        )
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "new deployment review did not open after local model handoff",
        )

    assert client.pin_calls == [{"local_path": "/agent/models/qwen"}]
    assert client.compose_params is not None
    assert client.compose_params["model_ref"] == "local-qwen"
    assert client.compose_params["model"] == "/agent/models/qwen"


class _CancelHandoffComposerClient:
    # Minimal composer for the handoff-cancel loop regressions (bug-250): a
    # cancel never reaches compose/validate/preview, so only the wizard-open
    # queries plus the handoff RPCs are stubbed. The pin/create/adopt RPCs
    # track calls (and must stay empty) so a cancel that wrongly triggered a
    # side effect fails loudly.
    connected = False

    def __init__(self) -> None:
        self.pin_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []
        self.adopt_calls: list[dict[str, object]] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def call(self, method: str, params):
        if method == "list_configs":
            return {"valid": [], "invalid": []}
        if method == "list_presets":
            return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
        if method == "list_deployment_recipes":
            return {"recipes": []}
        if method == "list_builds":
            return {"builds": [], "skipped": []}
        if method == "list_models":
            return {"models": []}
        if method == "check_build_prerequisites":
            return {"ok": True, "method": params.get("method"), "uv_available": True}
        if method == "pin_model":
            self.pin_calls.append(dict(params))
            return {"entry": {}}
        if method == "create_build":
            self.create_calls.append(dict(params))
            return {"job_id": params.get("job_id"), "kind": "create_build", "status": "running"}
        if method == "adopt_build":
            self.adopt_calls.append(dict(params))
            return {"build_id": "adopted"}
        if method == "discover_runs":
            return {"runs": []}
        if method in {"gpu", "sample_gpus"}:
            return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
        raise AssertionError(f"unexpected target client call: {method}")

    def subscribe(self, *_args, **_kwargs):
        raise AssertionError("a cancelled handoff should not subscribe")


async def _settle(pilot, cycles: int = 12) -> None:
    # Pump the message pump long enough for the deferred Select.Changed on a
    # freshly-restored draft to fire (or, on fixed code, to prove it does not).
    for _ in range(cycles):
        await pilot.pause()


@pytest.mark.asyncio
async def test_new_deployment_pin_hf_cancel_resumes_once_without_re_firing(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bug-250: choosing "Pin HF repo →" stashes model_mode="pin_hf" and opens the
    # dedicated pin screen. Cancelling it reopens the wizard with the raw draft;
    # before the fix the restored handoff mode re-fired the deferred
    # Select.Changed and re-dismissed the wizard straight back to the pin screen
    # in an inescapable loop. A cancel must reopen the wizard exactly ONCE, at
    # the Model step, with the draft intact, a NON-handoff model_mode, no
    # re-dismissal, and NO pin side effect.
    client = _CancelHandoffComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    pin_opens: list[object] = []
    real_pin = tui_app_module.PinModelScreen

    def _counting_pin(*args, **kwargs):
        screen = real_pin(*args, **kwargs)
        pin_opens.append(screen)
        return screen

    monkeypatch.setattr(tui_app_module, "PinModelScreen", _counting_pin)

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        # Walk to the Model step so the stashed draft's step_index is the Model
        # step — the real scenario the loop was observed in.
        await pilot.press("ctrl+n")  # Target → Runtime
        await pilot.press("ctrl+n")  # Runtime → Model
        await _wait_for_textual_condition(
            pilot,
            lambda: "Model"
            in str(app.screen.query_one("#new-deployment-current-step", Static).content),
            "wizard did not reach the Model step",
        )
        app.screen.query_one("#new-deployment-model-mode", Select).value = "pin_hf"
        await _wait_for_condition(
            lambda: app.screen.id == "pin-model" and bool(app.screen.query(Input)),
            "pin-HF handoff did not open the pin model flow",
        )
        assert len(pin_opens) == 1
        # Cancel the pin flow — the wizard reopens with the stashed draft.
        await pilot.press("escape")
        # On pre-fix code the reopened wizard re-fires the handoff and the pin
        # screen opens a SECOND time; wait for the dust to settle either way.
        await _wait_for_condition(
            lambda: len(pin_opens) >= 2 or app.screen.id == "new-deployment",
            "wizard did not reopen after cancelling the pin handoff",
        )
        await _settle(pilot)
        assert len(pin_opens) == 1, (
            f"cancelling the pin handoff re-opened the pin screen "
            f"{len(pin_opens)} times (cancel loop)"
        )
        assert app.screen.id == "new-deployment"
        assert "Model" in str(
            app.screen.query_one("#new-deployment-current-step", Static).content
        )
        mode = app.screen.query_one("#new-deployment-model-mode", Select).value
        assert mode not in {"pin_hf", "adopt_local"}
        assert app.screen.query_one("#new-deployment-model", Input).value == "Qwen/Qwen3-32B"
    assert client.pin_calls == []


@pytest.mark.asyncio
async def test_new_deployment_adopt_local_cancel_resumes_once_without_re_firing(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bug-250 sibling: the Adopt local path handoff stashes model_mode=
    # "adopt_local" and must resume once without re-firing, exactly like pin_hf.
    client = _CancelHandoffComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    pin_opens: list[object] = []
    real_pin = tui_app_module.PinModelScreen

    def _counting_pin(*args, **kwargs):
        screen = real_pin(*args, **kwargs)
        pin_opens.append(screen)
        return screen

    monkeypatch.setattr(tui_app_module, "PinModelScreen", _counting_pin)

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-model", Input).value = "/agent/models/qwen"
        await pilot.press("ctrl+n")  # Target → Runtime
        await pilot.press("ctrl+n")  # Runtime → Model
        await _wait_for_textual_condition(
            pilot,
            lambda: "Model"
            in str(app.screen.query_one("#new-deployment-current-step", Static).content),
            "wizard did not reach the Model step",
        )
        app.screen.query_one("#new-deployment-model-mode", Select).value = "adopt_local"
        await _wait_for_condition(
            lambda: app.screen.id == "pin-model" and bool(app.screen.query(Input)),
            "adopt-local handoff did not open the pin model flow",
        )
        assert len(pin_opens) == 1
        await pilot.press("escape")
        await _wait_for_condition(
            lambda: len(pin_opens) >= 2 or app.screen.id == "new-deployment",
            "wizard did not reopen after cancelling the adopt-local handoff",
        )
        await _settle(pilot)
        assert len(pin_opens) == 1, (
            f"cancelling the adopt-local handoff re-opened the pin screen "
            f"{len(pin_opens)} times (cancel loop)"
        )
        assert app.screen.id == "new-deployment"
        mode = app.screen.query_one("#new-deployment-model-mode", Select).value
        assert mode not in {"pin_hf", "adopt_local"}
        assert (
            app.screen.query_one("#new-deployment-model", Input).value
            == "/agent/models/qwen"
        )
    assert client.pin_calls == []


@pytest.mark.asyncio
async def test_new_deployment_create_build_cancel_resumes_without_re_firing(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bug-250 companion: the Runtime step's "Create build →" handoff must not
    # loop on cancel either. Its restore path already drops handoff runtime
    # values (only process/docker/build/executable are re-selected), so the
    # deferred Select.Changed never re-fires — this pins that non-looping
    # behavior (reopens once, no re-dismissal, no build side effect).
    client = _CancelHandoffComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    build_opens: list[object] = []
    real_build = tui_app_module.CreateBuildScreen

    def _counting_build(*args, **kwargs):
        screen = real_build(*args, **kwargs)
        build_opens.append(screen)
        return screen

    monkeypatch.setattr(tui_app_module, "CreateBuildScreen", _counting_build)

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        await pilot.press("ctrl+n")  # Target → Runtime
        await _wait_for_textual_condition(
            pilot,
            lambda: "Runtime"
            in str(app.screen.query_one("#new-deployment-current-step", Static).content),
            "wizard did not reach the Runtime step",
        )
        app.screen.query_one("#new-deployment-runtime", Select).value = "create_build"
        await _wait_for_condition(
            lambda: app.screen.id == "create-build" and bool(app.screen.query(Input)),
            "create-build handoff did not open",
        )
        assert len(build_opens) == 1
        await pilot.press("escape")
        await _wait_for_condition(
            lambda: len(build_opens) >= 2 or app.screen.id == "new-deployment",
            "wizard did not reopen after cancelling the create-build handoff",
        )
        await _settle(pilot)
        assert len(build_opens) == 1, (
            f"cancelling the create-build handoff re-opened the build screen "
            f"{len(build_opens)} times (cancel loop)"
        )
        assert app.screen.id == "new-deployment"
        runtime = app.screen.query_one("#new-deployment-runtime", Select).value
        assert runtime not in {"create_build", "adopt_build"}
    assert client.create_calls == []
    assert client.adopt_calls == []


@pytest.mark.asyncio
async def test_new_deployment_model_picker_shows_cache_and_gated_state(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "gated-qwen",
                            "display_name": "Gated Qwen",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3.6-27B-FP8",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "cache_state": "remote_only",
                            "gated": True,
                            "token_required": True,
                        }
                    ]
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model picker state test should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=ComposerClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-model-ref", Select).value = "gated-qwen"
        await pilot.pause()

        state = str(app.screen.query_one("#new-deployment-model-state", Static).content)
        assert "cache: remote_only" in state
        assert "auth: gated, requires HF_TOKEN" in state


@pytest.mark.asyncio
async def test_new_deployment_model_selection_shows_live_suggestions(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.suggest_calls: list[dict[str, object]] = []

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "capabilities": [
                    "compose_config",
                    "list_configs",
                    "list_models",
                    "list_presets",
                    "suggest_deployment_defaults",
                ]
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "qwen-fp8",
                            "display_name": "Qwen FP8",
                            "source": "hf_repo",
                            "repo_id": "Qwen/Qwen3.6-27B-FP8",
                            "revision": "main",
                            "commit_sha": "abc123",
                            "cache_state": "remote_only",
                            "gated": True,
                            "token_required": True,
                        }
                    ]
                }
            if method == "suggest_deployment_defaults":
                self.suggest_calls.append(dict(params))
                return {
                    "engine_suggestions": {
                        "dtype": "auto",
                        "kv_cache_dtype": "fp8",
                        "tensor_parallel_size": 2,
                    },
                    "warnings": ["gated-needs-token"],
                    "sources": ["model_registry"],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("suggestion test should not subscribe")

    client = ComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-model-ref", Select).value = "qwen-fp8"
        await _wait_for_condition(
            lambda: "kv_cache_dtype=fp8"
            in str(app.screen.query_one("#new-deployment-model-suggestions", Static).content),
            "live model suggestions did not render for registry pin",
        )

        suggestions = str(
            app.screen.query_one("#new-deployment-model-suggestions", Static).content
        )
        assert "dtype=auto" in suggestions
        assert "tensor_parallel_size=2" in suggestions
        assert "gated-needs-token" in suggestions

    assert client.suggest_calls
    assert client.suggest_calls[-1]["model_ref"] == "qwen-fp8"
    assert client.suggest_calls[-1]["revision"] == "abc123"


@pytest.mark.asyncio
async def test_new_deployment_bare_model_shows_live_suggestions(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.suggest_calls: list[dict[str, object]] = []

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "capabilities": [
                    "compose_config",
                    "list_configs",
                    "list_models",
                    "list_presets",
                    "suggest_deployment_defaults",
                ]
            }

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_models":
                return {"models": []}
            if method == "suggest_deployment_defaults":
                self.suggest_calls.append(dict(params))
                return {
                    "engine_suggestions": {
                        "dtype": "auto",
                        "kv_cache_dtype": "fp8",
                        "tensor_parallel_size": 1,
                    },
                    "warnings": ["gated-needs-token"],
                    "sources": ["hf_config"],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("bare suggestion test should not subscribe")

    client = ComposerClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-model", Input).value = (
            "Qwen/Qwen3.6-27B-FP8"
        )
        await _wait_for_condition(
            lambda: "kv_cache_dtype=fp8"
            in str(app.screen.query_one("#new-deployment-model-suggestions", Static).content),
            "live model suggestions did not render for bare model",
        )

        suggestions = str(
            app.screen.query_one("#new-deployment-model-suggestions", Static).content
        )
        assert "dtype=auto" in suggestions
        assert "tensor_parallel_size=1" in suggestions
        assert "gated-needs-token" in suggestions

    assert client.suggest_calls
    assert client.suggest_calls[-1]["model"] == "Qwen/Qwen3.6-27B-FP8"


@pytest.mark.asyncio
async def test_new_deployment_save_uses_composer_rpc_path(config_dir: Path) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict | None]] = []
            self.saved_config: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                if self.saved_config is None:
                    return {"valid": [], "invalid": []}
                return {
                    "valid": [
                        {
                            "path": str(config_dir / "qwen3.yaml"),
                            "name": "qwen3",
                            "model": "Qwen/Qwen3-32B",
                            "target": "local",
                            "warnings": [],
                            "config": self.saved_config,
                        }
                    ],
                    "invalid": [],
                }
            if method == "list_presets":
                return {
                    "presets": [
                        {
                            "name": "balanced",
                            "description": "Balanced",
                            "engine": {},
                            "extra_args": [],
                            "applies_to": ["all"],
                        }
                    ]
                }
            if method == "compose_config":
                assert params["name"] == "qwen3"
                assert params["model"] == "Qwen/Qwen3-32B"
                assert params["runtime"] == "process"
                assert params["overrides"]["server"]["port"] == 18001
                return {
                    "config": {
                        "name": "qwen3",
                        "target": "local",
                        "model": "Qwen/Qwen3-32B",
                        "server": {
                            "host": "127.0.0.1",
                            "port": 18001,
                            "exposure": "local",
                        },
                    },
                    "warnings": [],
                    "derived": [
                        {
                            "field": "server.port",
                            "value": "18001",
                            "source": "operator",
                        }
                    ],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "save_config":
                self.saved_config = dict(params["config"])
                return {
                    "path": str(config_dir / "qwen3.yaml"),
                    "name": "qwen3",
                    "config": self.saved_config,
                }
            if method == "preview":
                if "config" in params:
                    assert params["config"]["name"] == "qwen3"
                else:
                    assert params["name"] == "qwen3"
                return {
                    "preview": "cwd=/agent\nvllm serve Qwen/Qwen3-32B",
                    "warnings": ["preview-warning"],
                }
            if method == "preflight":
                assert params["config"]["name"] == "qwen3"
                return {"ok": True, "failures": []}
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment save should not subscribe")

    app = VelaApp(configs_dir=config_dir, target_client=ComposerClient())

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        # Clean walk (A3): the optional-section RPCs returned empty, so no
        # #new-deployment-*-warning row is displayed.
        for _warn_id in (
            "#new-deployment-recipe-warning",
            "#new-deployment-model-warning",
            "#new-deployment-build-warning",
        ):
            assert app.screen.query_one(_warn_id, Static).display is False
        app.screen.query_one("#new-deployment-name", Input).value = "qwen3"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#new-deployment-port", Input).value = "18001"
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()

        assert app.screen.id == "new-deployment-review"
        assert "vllm serve Qwen/Qwen3-32B" in app.screen.query_one(
            "#new-deployment-review-preview", Static
        ).content
        assert "server.port" in app.screen.query_one(
            "#new-deployment-review-derived", Static
        ).content
        assert app.current_config is None

        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()

        assert app.current_config is not None
        assert app.current_config.name == "qwen3"
        assert app.selected_config_preview == "cwd=/agent\nvllm serve Qwen/Qwen3-32B"


@pytest.mark.asyncio
async def test_new_deployment_review_preflights_draft_before_save(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict | None]] = []
            self.preflighted = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "compose_config":
                return {
                    "config": {
                        "name": "qwen3",
                        "target": "local",
                        "model": "Qwen/Qwen3-32B",
                        "command": {"executable": sys.executable},
                        "server": {
                            "host": "127.0.0.1",
                            "port": 18001,
                            "exposure": "local",
                        },
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve Qwen/Qwen3-32B", "warnings": []}
            if method == "preflight":
                assert params["config"]["name"] == "qwen3"
                self.preflighted = True
                return {"ok": True, "failures": []}
            if method == "save_config":
                assert self.preflighted is True
                return {
                    "path": str(config_dir / "qwen3.yaml"),
                    "name": "qwen3",
                    "config": dict(params["config"]),
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment save should not subscribe")

    client = ComposerClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#new-deployment-name", Input).value = "qwen3"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#new-deployment-port", Input).value = "18001"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.screen.id == "new-deployment-review"

        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()

    methods = [method for method, _params in client.calls]
    assert methods.index("preflight") < methods.index("save_config")


@pytest.mark.asyncio
async def test_new_deployment_review_customizes_draft_with_flag_manager(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict | None]] = []
            self.saved_config: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                if self.saved_config is None:
                    return {"valid": [], "invalid": []}
                return {
                    "valid": [
                        {
                            "path": str(config_dir / "qwen3.yaml"),
                            "name": "qwen3",
                            "model": "Qwen/Qwen3-32B",
                            "target": "local",
                            "warnings": [],
                            "config": self.saved_config,
                        }
                    ],
                    "invalid": [],
                }
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "compose_config":
                return {
                    "config": {
                        "name": "qwen3",
                        "target": "local",
                        "model": "Qwen/Qwen3-32B",
                        "engine": {"tensor_parallel_size": 2},
                        "server": {
                            "host": "127.0.0.1",
                            "port": 18001,
                            "exposure": "local",
                        },
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                config = params.get("config") if isinstance(params, dict) else {}
                engine = config.get("engine") if isinstance(config, dict) else {}
                tensor_parallel_size = (
                    engine.get("tensor_parallel_size")
                    if isinstance(engine, dict)
                    else 2
                )
                return {
                    "preview": (
                        "cwd=/agent\n"
                        f"vllm serve Qwen/Qwen3-32B --tensor-parallel-size "
                        f"{tensor_parallel_size}"
                    ),
                    "warnings": [],
                    "metadata": {
                        "known_flags": ["--tensor-parallel-size"],
                        "flag_map": {"tensor_parallel_size": "--tensor-parallel-size"},
                    },
                }
            if method == "preflight":
                assert params["config"]["engine"]["tensor_parallel_size"] == 4
                return {"ok": True, "failures": []}
            if method == "save_config":
                self.saved_config = dict(params["config"])
                assert self.saved_config["engine"]["tensor_parallel_size"] == 4
                return {
                    "path": str(config_dir / "qwen3.yaml"),
                    "name": "qwen3",
                    "config": self.saved_config,
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment flag manager should not subscribe")

    client = ComposerClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#new-deployment-name", Input).value = "qwen3"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#new-deployment-port", Input).value = "18001"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.screen.id == "new-deployment-review"

        await pilot.press("f")
        await _wait_for_condition(
            lambda: app.screen.id == "flag-manager",
            "new deployment flag manager did not open",
        )
        value_input = app.screen.query_one("#flag-manager-value", Input)
        value_input.value = "4"
        await _wait_for_condition(
            lambda: "--tensor-parallel-size 4"
            in str(app.screen.query_one("#flag-manager-detail", Static).content),
            "new deployment draft preview did not update after flag edit",
        )
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review"
            and "--tensor-parallel-size 4"
            in str(app.screen.query_one("#new-deployment-review-preview", Static).content),
            "customized deployment review did not render updated preview",
        )
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()

    assert client.saved_config is not None
    assert client.saved_config["engine"]["tensor_parallel_size"] == 4


@pytest.mark.asyncio
async def test_new_deployment_review_save_and_smoke_launches_saved_config(
    config_dir: Path,
) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict | None]] = []
            self.saved_config: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                if self.saved_config is None:
                    return {"valid": [], "invalid": []}
                return {
                    "valid": [
                        {
                            "path": str(config_dir / "qwen3.yaml"),
                            "name": "qwen3",
                            "model": "Qwen/Qwen3-32B",
                            "target": "local",
                            "warnings": [],
                            "config": self.saved_config,
                        }
                    ],
                    "invalid": [],
                }
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "compose_config":
                return {
                    "config": {
                        "name": "qwen3",
                        "target": "local",
                        "model": "Qwen/Qwen3-32B",
                        "server": {
                            "host": "127.0.0.1",
                            "port": 18001,
                            "exposure": "local",
                        },
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve Qwen/Qwen3-32B", "warnings": []}
            if method == "preflight":
                if "config" in params:
                    assert params["config"]["name"] == "qwen3"
                else:
                    assert params["name"] == "qwen3"
                return {"ok": True, "failures": []}
            if method == "save_config":
                self.saved_config = dict(params["config"])
                return {
                    "path": str(config_dir / "qwen3.yaml"),
                    "name": "qwen3",
                    "config": self.saved_config,
                }
            if method == "prepare_launch":
                assert params["name"] == "qwen3"
                return {
                    "config": {
                        "name": "qwen3",
                        "target": "local",
                        "model": "Qwen/Qwen3-32B",
                        "server": {"host": "127.0.0.1", "port": 18001},
                    },
                    "build": {
                        "argv": ["vllm", "serve", "Qwen/Qwen3-32B"],
                        "env": {},
                        "cwd": str(config_dir),
                        "warnings": [],
                        "metadata": {"vllm_version_profile": "current"},
                        "preview": "cwd=/agent\nvllm serve Qwen/Qwen3-32B",
                    },
                    "preflight": None,
                }
            if method == "launch":
                assert params["name"] == "qwen3"
                return {"run_id": "smoke-run", "launch_mode": "attached", "status": "started"}
            if method == "probe_until_ready":
                assert params["run_id"] == "smoke-run"
                return {
                    "run_id": "smoke-run",
                    "ready": True,
                    "detail": "ready",
                    "models": ["qwen3"],
                    "reachable_url": "http://127.0.0.1:18001",
                    "error_kind": None,
                }
            if method == "stop":
                assert params["run_id"] == "smoke-run"
                return {"run_id": "smoke-run", "status": "stopped"}
            if method == "wait":
                assert params["run_id"] == "smoke-run"
                return {"run_id": "smoke-run", "returncode": 0, "intentional": False}
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "smoke-run",
                "returncode": 0,
                "intentional": False,
                "phase": Phase.STOPPED.value,
                "seq": 1,
                "ts": "2026-06-06T00:00:00Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["smoke-run"]
            assert resume_from == "live"
            return self._events()

    client = ComposerClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#new-deployment-name", Input).value = "qwen3"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#new-deployment-port", Input).value = "18001"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.screen.id == "new-deployment-review"

        await pilot.press("s")
        await _wait_for_condition(
            lambda: app.phase is Phase.STOPPED,
            "save and smoke did not drive the launch lifecycle",
        )

    methods = [method for method, _params in client.calls]
    assert methods.index("save_config") < methods.index("prepare_launch")
    assert methods.index("prepare_launch") < methods.index("launch")
    assert methods.index("launch") < methods.index("probe_until_ready")
    assert methods.index("probe_until_ready") < methods.index("stop")
    assert methods.index("stop") < methods.index("wait")


@pytest.mark.asyncio
async def test_new_deployment_save_and_smoke_walks_fake_docker_to_ready(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    docker = tmp_path / "docker"
    docker_log = tmp_path / "docker-commands.log"
    docker_state = tmp_path / "docker-state"
    write_fake_docker_runtime(docker)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_DOCKER_COMMAND_LOG", str(docker_log))
    monkeypatch.setenv("FAKE_DOCKER_STATE_FILE", str(docker_state))
    monkeypatch.setenv("FAKE_DOCKER_WAIT_SECONDS", "10")

    async def ready_probe_loop(cfg, *, emit, is_process_alive, **_kwargs) -> None:
        emit(HealthEvent(ready=True, detail="ready", models=[cfg.served_model_name]))

    monkeypatch.setattr(agent_local_module, "probe_loop", ready_probe_loop)

    app = VelaApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#new-deployment-runtime", Select).value = "docker"
        app.screen.query_one("#new-deployment-image", Input).value = (
            "vllm/vllm-openai@sha256:image"
        )
        app.screen.query_one("#new-deployment-port", Input).value = str(unused_tcp_port)
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "new deployment review did not open for fake docker smoke",
        )

        await pilot.press("s")
        await _wait_for_condition(
            lambda: app.phase is Phase.STOPPED,
            "fake docker save-and-smoke did not reach stopped phase",
        )

    docker_commands = docker_log.read_text(encoding="utf-8")
    assert "image inspect vllm/vllm-openai@sha256:image" in docker_commands
    assert "run -d --name" in docker_commands
    assert "logs -f container-123" in docker_commands
    assert "stop -t 90 container-123" in docker_commands
    assert app.ready_url == f"http://127.0.0.1:{unused_tcp_port}"
    assert app.served_models == ["Qwen3-32B"]


@pytest.mark.asyncio
async def test_new_deployment_save_and_smoke_surfaces_named_failure(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    docker = tmp_path / "docker"
    docker_log = tmp_path / "docker-commands.log"
    docker_state = tmp_path / "docker-state"
    write_fake_docker_runtime(docker)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_DOCKER_COMMAND_LOG", str(docker_log))
    monkeypatch.setenv("FAKE_DOCKER_STATE_FILE", str(docker_state))
    monkeypatch.setenv("FAKE_DOCKER_WAIT_SECONDS", "10")

    async def auth_failure_probe_loop(cfg, *, emit, is_process_alive, **_kwargs) -> None:
        emit(
            HealthEvent(
                ready=False,
                detail="HF token missing for gated model",
                error_kind=ErrorKind.HF_AUTH,
            )
        )

    monkeypatch.setattr(agent_local_module, "probe_loop", auth_failure_probe_loop)

    app = VelaApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 48)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment",
            "new deployment screen did not open",
        )
        app.screen.query_one("#new-deployment-name", Input).value = "qwen"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#new-deployment-runtime", Select).value = "docker"
        app.screen.query_one("#new-deployment-image", Input).value = (
            "vllm/vllm-openai@sha256:image"
        )
        app.screen.query_one("#new-deployment-port", Input).value = str(unused_tcp_port)
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "new deployment review did not open for fake docker smoke failure",
        )

        await pilot.press("s")
        await _wait_for_condition(
            lambda: app.phase is Phase.STOPPED,
            "fake docker smoke failure did not stop",
        )

    assert "HF_AUTH" in app.error_text
    assert "set HF_TOKEN in the target agent" in app.error_text
    assert "HF token missing for gated model" in app.error_text
    assert "Smoke did not reach READY" not in app.error_text
    assert "stop -t 90 container-123" in docker_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_new_deployment_review_cancel_does_not_write(config_dir: Path) -> None:
    class ComposerClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict | None]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "compose_config":
                return {
                    "config": {
                        "name": "qwen3",
                        "target": "local",
                        "model": "Qwen/Qwen3-32B",
                    },
                    "warnings": [],
                    "derived": [],
                }
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve Qwen/Qwen3-32B", "warnings": []}
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "save_config":
                raise AssertionError("cancelled deployment review should not save")
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment review should not subscribe")

    app = VelaApp(configs_dir=config_dir, target_client=ComposerClient())

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#new-deployment-name", Input).value = "qwen3"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert app.screen.id == "new-deployment-review"

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen.id != "new-deployment-review"
        assert app.current_config is None


@pytest.mark.asyncio
async def test_prompt_and_picker_screens_render_as_modal_panels(config_dir: Path) -> None:
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ModalScreen)
        picker_panel = app.screen.query_one("#config-picker-panel")
        assert picker_panel.region.x > 0

        await pilot.press("escape")
        await pilot.press("/")
        await pilot.pause()
        assert isinstance(app.screen, ModalScreen)
        prompt_panel = app.screen.query_one("#log-prompt-panel")
        assert prompt_panel.region.x > 0
        assert prompt_panel.region.height < app.size.height // 2


@pytest.mark.asyncio
async def test_config_picker_marks_invalid_configs_with_warning_glyph(
    config_dir: Path,
) -> None:
    write_yaml(config_dir / "good.yaml", "name: good\nmodel: org/model")
    write_yaml(config_dir / "bad.yaml", "name: bad")
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ConfirmScreen("Attached server is still running. Stop it?"))
        await pilot.pause()

        assert app.screen.id == "confirm"
        assert isinstance(app.screen, ModalScreen)
        confirm_panel = app.screen.query_one("#confirm-panel")
        assert confirm_panel.region.x > 0
        assert confirm_panel.region.height < app.size.height // 2
        message = app.screen.query_one("#confirm-message", Static)
        assert isinstance(message.content, Text)
        assert "Stop" in message.content.plain
        assert "Cancel" in message.content.plain
        assert _text_uses_style(message.content, tui_app_module.BAD)
        assert _text_uses_style(message.content, tui_app_module.GOOD)


def test_confirm_and_log_prompt_use_canonical_tokens() -> None:
    # bug-237 item 9: confirm.py / log_prompt.py migrate their same-hex legacy
    # tokens (ACCENT/GOOD/WARN/MUTED/TEXT) to the canonical theme names. BAD is
    # intentionally kept (the canonical RED differs in hex and the app-wide
    # destructive color is still BAD). Visual-only: the rendered hex is identical.
    import vela.tui.screens.confirm as confirm_mod
    import vela.tui.screens.log_prompt as log_prompt_mod

    for legacy in ("ACCENT", "GOOD", "WARN", "MUTED", "TEXT"):
        assert not hasattr(confirm_mod, legacy), f"confirm still binds legacy {legacy}"
    for canonical in ("CYAN", "GREEN", "AMBER", "TEXT_SECONDARY", "TEXT_PRIMARY"):
        assert hasattr(confirm_mod, canonical), f"confirm missing canonical {canonical}"
    # BAD stays (destructive color, matches the app + target_edit).
    assert hasattr(confirm_mod, "BAD")

    assert not hasattr(log_prompt_mod, "TEXT"), "log_prompt still binds legacy TEXT"
    assert hasattr(log_prompt_mod, "TEXT_PRIMARY")


@pytest.mark.asyncio
async def test_quit_and_kill_do_not_stack_a_second_confirm(config_dir: Path) -> None:
    # bug-279 (1.3 carry-forward): the palette 'Quit app' / 'Kill' (or any direct
    # invocation) fired while a ConfirmScreen is already open used to push a
    # SECOND screen with the same id='confirm' -> a DuplicateIds crash. The
    # openers now no-op when a ConfirmScreen is on top: exactly one confirm.
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"  # _attached_run_is_alive() -> True

        await pilot.press("q")
        await pilot.pause()
        assert app.screen.id == "confirm"
        assert len([s for s in app.screen_stack if isinstance(s, ConfirmScreen)]) == 1

        app.action_quit()  # palette 'Quit app' while the confirm is open
        await pilot.pause()
        assert app.is_running
        assert len([s for s in app.screen_stack if isinstance(s, ConfirmScreen)]) == 1

        app.action_kill()  # palette 'Kill' while the confirm is open
        await pilot.pause()
        assert app.is_running
        assert len([s for s in app.screen_stack if isinstance(s, ConfirmScreen)]) == 1


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

    app = VelaApp(
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

    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir, target_client=TargetClient())

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
async def test_config_picker_panel_hugs_content_with_shared_frame(config_dir: Path) -> None:
    # bug-237: the picker panel must hug its content (height: auto via the
    # shared modal frame), not fill a fixed-height mostly-empty box.
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        panel = app.screen.query_one("#config-picker-panel")
        assert panel.styles.height.is_auto
        # one config -> hug a handful of rows, nowhere near the old fixed
        # max-height (32) or the 40-row terminal.
        assert panel.region.height < 20
        assert panel.region.x > 0
        assert panel.region.right <= 80


@pytest.mark.asyncio
async def test_config_picker_keeps_selection_in_view_when_scrolling(config_dir: Path) -> None:
    # bug-237: arrowing past the fold must scroll the list so the '>' marker
    # stays visible instead of being lost below the fold.
    for index in range(30):
        write_yaml(
            config_dir / f"cfg{index:02d}.yaml",
            f"name: cfg{index:02d}\nmodel: org/cfg{index:02d}",
        )
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        scroll = app.screen.query_one("#config-picker-scroll")

        def marker_visible() -> bool:
            line = app.screen._selected_line
            top = scroll.scroll_offset.y
            return line is not None and top <= line < top + scroll.size.height

        assert marker_visible()
        for _ in range(29):
            await pilot.press("down")
        await pilot.pause()
        # selection is at the bottom: the scroller moved past the fold AND the
        # marker is still on screen.
        assert scroll.scroll_offset.y > 0
        assert marker_visible()
        for _ in range(29):
            await pilot.press("up")
        await pilot.pause()
        assert marker_visible()


@pytest.mark.asyncio
async def test_config_picker_enter_with_no_matches_stays_open(config_dir: Path) -> None:
    # bug-237: Enter with a filter that matches nothing must NOT silently
    # dismiss the picker; it stays open with an Esc hint.
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.press("c")
        await pilot.press("z", "z", "z", "z")
        await pilot.pause()
        assert "no match — Esc to close" in app.screen.summary
        await pilot.press("enter")
        await pilot.pause()
        # Enter with zero matches keeps the picker open instead of vanishing.
        assert app.screen.id == "config-picker"


@pytest.mark.asyncio
async def test_config_picker_empty_state_tells_you_to_close_first(config_dir: Path) -> None:
    # bug-237: the focused filter Input eats 'n', so the first-run empty copy
    # must tell the user to close the picker before pressing n on the dashboard.
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.id == "config-picker"
        assert "press n on the dashboard" in app.screen.summary
        assert "close" in app.screen.summary.lower()


@pytest.mark.asyncio
async def test_config_picker_offline_empty_state_is_amber_and_honest(config_dir: Path) -> None:
    # bug-237 / bug-252 carry-forward: offline with nothing cached must say the
    # target is unreachable (amber), not the first-run "No configs yet" copy.
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            config_picker_module.ConfigPickerScreen(
                app.registry, connection_state="disconnected"
            )
        )
        await pilot.pause()
        assert app.screen.id == "config-picker"
        summary = app.screen.summary
        assert "target unreachable — configs unknown" in summary
        assert "No configs yet" not in summary
        content = app.screen.query_one("#config-picker-list", Static).content
        assert isinstance(content, Text)
        assert _text_uses_style(content, tui_app_module.WARN)


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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(
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
        # Title moved to the panel-topmost #flag-manager-title (bug-237).
        title = str(app.screen.query_one("#flag-manager-title", Static).content)
        assert "Flag Manager" in title
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
async def test_flag_manager_edits_raw_passthrough_args(
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
        "    print('  --tensor-parallel-size INTEGER')\n"
        "    print('  --moe-backend TEXT')\n"
        "    print('  --max-num-batched-tokens INTEGER')\n",
        encoding="utf-8",
    )
    fake_vllm.chmod(0o755)
    write_yaml(
        config_dir / "raw-flags.yaml",
        f"""
        name: raw-flags
        model: org/model
        command:
          executable: {fake_vllm}
        engine:
          tensor_parallel_size: 2
        extra_args:
          - --moe-backend
          - flashinfer_cutlass
        """,
    )
    app = VelaApp(
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
        raw_input = app.screen.query_one("#flag-manager-extra-args", Input)
        assert "--moe-backend flashinfer_cutlass" in raw_input.value

        raw_input.value = (
            "--moe-backend flashinfer_cutlass "
            "--max-num-batched-tokens 8192"
        )
        await _wait_for_condition(
            lambda: "--max-num-batched-tokens 8192"
            in str(app.screen.query_one("#flag-manager-detail", Static).content),
            "flag manager preview did not refresh with raw passthrough args",
        )

        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "_default",
            "flag manager did not close after saving raw passthrough args",
        )

    assert app.current_config is not None
    assert app.current_config.extra_args == [
        "--moe-backend",
        "flashinfer_cutlass",
        "--max-num-batched-tokens",
        "8192",
    ]
    text = (config_dir / "raw-flags.yaml").read_text(encoding="utf-8")
    assert "--max-num-batched-tokens" in text


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

    app = VelaApp(configs_dir=config_dir, target_client=TargetClient())

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
async def test_flag_manager_preset_switch_reseeds_engine_fields(
    config_dir: Path,
) -> None:
    class TargetClient:
        connected = False

        def __init__(self) -> None:
            self.preview_calls: list[dict[str, object]] = []

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "capabilities": [
                    "list_configs",
                    "list_presets",
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
                            "path": "/agent/configs/preset.yaml",
                            "name": "preset",
                            "model": "org/model",
                            "target": None,
                            "warnings": [],
                            "config": {
                                "name": "preset",
                                "model": "org/model",
                                "engine": {
                                    "gpu_memory_utilization": 0.9,
                                    "dtype": "auto",
                                    "max_num_seqs": 4,
                                },
                            },
                        }
                    ],
                    "invalid": [],
                }
            if method == "list_presets":
                return {
                    "presets": [
                        {
                            "name": "balanced",
                            "description": "",
                            "engine": {
                                "gpu_memory_utilization": 0.9,
                                "dtype": "auto",
                                "max_num_seqs": 4,
                            },
                        },
                        {
                            "name": "throughput",
                            "description": "",
                            "engine": {
                                "gpu_memory_utilization": 0.92,
                                "dtype": "auto",
                                "max_num_seqs": 32,
                            },
                        },
                    ]
                }
            if method == "preview":
                engine = {
                    "gpu_memory_utilization": 0.9,
                    "dtype": "auto",
                    "max_num_seqs": 4,
                }
                updates = params.get("engine") if isinstance(params, dict) else None
                if isinstance(updates, dict):
                    engine.update(updates)
                self.preview_calls.append(dict(engine))
                return {
                    "preview": (
                        "cwd=/agent\n"
                        "vllm serve org/model "
                        f"--gpu-memory-utilization {engine['gpu_memory_utilization']} "
                        f"--dtype {engine['dtype']} "
                        f"--max-num-seqs {engine['max_num_seqs']}"
                    ),
                    "warnings": [],
                    "metadata": {
                        "known_flags": [
                            "--gpu-memory-utilization",
                            "--dtype",
                            "--max-num-seqs",
                        ],
                        "flag_map": {
                            "gpu_memory_utilization": "--gpu-memory-utilization",
                            "dtype": "--dtype",
                            "max_num_seqs": "--max-num-seqs",
                        },
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
    app = VelaApp(configs_dir=config_dir, target_client=target_client)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        await _wait_for_condition(
            lambda: app.screen.id == "flag-manager",
            "flag manager did not open",
        )

        app.screen.query_one("#flag-manager-preset", Select).value = "throughput"
        await _wait_for_condition(
            lambda: "--max-num-seqs 32"
            in str(app.screen.query_one("#flag-manager-detail", Static).content),
            "preset switch did not refresh the draft preview",
        )

    assert any(call.get("max_num_seqs") == 32 for call in target_client.preview_calls)


@pytest.mark.asyncio
async def test_flag_manager_reset_to_preset_value(
    config_dir: Path,
) -> None:
    class TargetClient:
        connected = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "capabilities": [
                    "list_configs",
                    "list_presets",
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
                            "path": "/agent/configs/preset-reset.yaml",
                            "name": "preset-reset",
                            "model": "org/model",
                            "target": None,
                            "warnings": [],
                            "config": {
                                "name": "preset-reset",
                                "model": "org/model",
                                "engine": {"gpu_memory_utilization": 0.88},
                            },
                        }
                    ],
                    "invalid": [],
                }
            if method == "list_presets":
                return {
                    "presets": [
                        {
                            "name": "balanced",
                            "description": "",
                            "engine": {"gpu_memory_utilization": 0.9},
                        }
                    ]
                }
            if method == "preview":
                engine = {"gpu_memory_utilization": 0.88}
                updates = params.get("engine") if isinstance(params, dict) else None
                if isinstance(updates, dict):
                    engine.update(updates)
                return {
                    "preview": (
                        "cwd=/agent\n"
                        "vllm serve org/model --gpu-memory-utilization "
                        f"{engine['gpu_memory_utilization']}"
                    ),
                    "warnings": [],
                    "metadata": {
                        "known_flags": ["--gpu-memory-utilization"],
                        "flag_map": {
                            "gpu_memory_utilization": "--gpu-memory-utilization"
                        },
                    },
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("flag manager should not subscribe")

    app = VelaApp(configs_dir=config_dir, target_client=TargetClient())

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        await _wait_for_condition(
            lambda: app.screen.id == "flag-manager",
            "flag manager did not open",
        )

        value_input = app.screen.query_one("#flag-manager-value", Input)
        value_input.value = "0.95"
        await _wait_for_condition(
            lambda: "--gpu-memory-utilization 0.95"
            in str(app.screen.query_one("#flag-manager-detail", Static).content),
            "flag manager preview did not refresh with edited value",
        )
        await pilot.press("p")
        await _wait_for_condition(
            lambda: (
                "--gpu-memory-utilization 0.9"
                in str(app.screen.query_one("#flag-manager-detail", Static).content)
                and "--gpu-memory-utilization 0.95"
                not in str(app.screen.query_one("#flag-manager-detail", Static).content)
            ),
            "reset-to-preset did not restore the selected preset value",
        )


@pytest.mark.asyncio
async def test_flag_manager_changed_only_filter_hides_unchanged_fields(
    config_dir: Path,
) -> None:
    class TargetClient:
        connected = False

        async def connect(self) -> dict[str, object]:
            self.connected = True
            return {
                "capabilities": [
                    "list_configs",
                    "list_presets",
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
                            "path": "/agent/configs/changed.yaml",
                            "name": "changed",
                            "model": "org/model",
                            "target": None,
                            "warnings": [],
                            "config": {
                                "name": "changed",
                                "model": "org/model",
                                "engine": {
                                    "gpu_memory_utilization": 0.88,
                                    "dtype": "auto",
                                },
                            },
                        }
                    ],
                    "invalid": [],
                }
            if method == "list_presets":
                return {
                    "presets": [
                        {
                            "name": "balanced",
                            "description": "",
                            "engine": {
                                "gpu_memory_utilization": 0.9,
                                "dtype": "auto",
                            },
                        }
                    ]
                }
            if method == "preview":
                return {
                    "preview": "cwd=/agent\nvllm serve org/model",
                    "warnings": [],
                    "metadata": {
                        "known_flags": ["--gpu-memory-utilization", "--dtype"],
                        "flag_map": {
                            "gpu_memory_utilization": "--gpu-memory-utilization",
                            "dtype": "--dtype",
                        },
                    },
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("flag manager should not subscribe")

    app = VelaApp(configs_dir=config_dir, target_client=TargetClient())

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        await _wait_for_condition(
            lambda: app.screen.id == "flag-manager",
            "flag manager did not open",
        )

        before = str(app.screen.query_one("#flag-manager-list", Static).content)
        assert "gpu-memory-utilization = 0.88" in before
        assert "dtype = auto" in before

        app.screen.query_one("#flag-manager-changed-only", Checkbox).value = True
        await _wait_for_condition(
            lambda: "dtype = auto"
            not in str(app.screen.query_one("#flag-manager-list", Static).content),
            "changed-only filter did not hide unchanged fields",
        )
        after = str(app.screen.query_one("#flag-manager-list", Static).content)
        assert "gpu-memory-utilization = 0.88" in after


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
    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir, target_client=target_client)

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
    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(configs_dir=config_dir, clock=clock)

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
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        badge = app.query_one("#status-badge")
        status_dot = app.query_one("#status-dot", Static)
        status_label = app.query_one("#status-label", Static)

        assert app.status_text == "○ IDLE"
        assert badge.has_class("status--idle")
        assert status_dot.content.plain == "○"
        assert status_label.content.plain == "IDLE"

        app._set_phase(Phase.STARTING)
        assert app.status_text == "● STARTING"
        assert badge.has_class("status--loading")
        assert badge.has_class("status--pulse")
        assert status_dot.content.plain == "●"
        assert status_label.content.plain == "STARTING"

        app._set_phase(Phase.READY)
        assert app.status_text.startswith("● READY")
        assert badge.has_class("status--ready")
        assert not badge.has_class("status--pulse")
        assert status_dot.content.plain == "●"
        assert status_label.content.plain == "READY"

        app._set_phase(Phase.ERROR)
        assert app.status_text == "✕ ERROR"
        assert badge.has_class("status--error")
        assert status_dot.content.plain == "✕"
        assert status_label.content.plain == "ERROR"


@pytest.mark.asyncio
async def test_with_agent_busy_shows_pulsing_verb_then_restores(
    config_dir: Path,
) -> None:
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()
        badge = app.query_one("#status-badge")
        status_label = app.query_one("#status-label", Static)
        assert app.phase is Phase.IDLE
        assert status_label.content.plain == "IDLE"

        gate = asyncio.Event()

        async def gated_rpc() -> dict[str, str]:
            await gate.wait()
            return {"models": "ok"}

        task = asyncio.ensure_future(
            app._with_agent_busy("loading models…", gated_rpc())
        )
        # Mid-flight: the RPC is parked on the gate, so the busy overlay holds.
        await pilot.pause()
        assert badge.has_class("status--loading")
        assert badge.has_class("status--pulse")
        assert status_label.content.plain == "loading models…"

        gate.set()
        result = await task
        await pilot.pause()

        assert result == {"models": "ok"}
        # Restored to the live (idle) phase after success.
        assert not badge.has_class("status--pulse")
        assert badge.has_class("status--idle")
        assert status_label.content.plain == "IDLE"


@pytest.mark.asyncio
async def test_busy_badge_refits_header_chrome_for_wide_verbs(config_dir: Path) -> None:
    # 4.5 carry-forward: a wide busy verb grows the badge box; without a chrome
    # re-fit the 1fr model slot keeps its stale wider text and the 80-col
    # header wraps. _busy_badge must refresh the chrome on paint AND restore.
    write_yaml(
        config_dir / "long.yaml",
        """
        name: a-very-long-config-name
        model: organization/a-really-long-model-name-that-fills-the-header
        """,
    )
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        model = app.query_one("#active-model", Static)
        text_before = str(model.content)
        assert text_before

        gate = asyncio.Event()

        async def gated_rpc() -> dict[str, bool]:
            await gate.wait()
            return {"ok": True}

        task = asyncio.ensure_future(
            app._with_agent_busy("a deliberately wide busy verb…", gated_rpc())
        )
        await pilot.pause()
        text_busy = str(model.content)
        assert cell_len(text_busy) < cell_len(text_before), (
            "header model slot was not re-fit while the wide busy verb was up"
        )

        gate.set()
        await task
        await pilot.pause()
        # Restore re-fits too: the model text expands back to its idle width.
        assert str(model.content) == text_before


@pytest.mark.asyncio
async def test_with_agent_busy_renders_banner_and_returns_none_on_target_error(
    config_dir: Path,
) -> None:
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()
        badge = app.query_one("#status-badge")
        status_label = app.query_one("#status-label", Static)

        async def failing_rpc() -> dict[str, str]:
            raise TargetCallError("agent-unreachable", "target unreachable")

        result = await app._with_agent_busy("loading models…", failing_rpc())
        await pilot.pause()

        # Sentinel None so callers abort opening a screen without crashing.
        assert result is None
        # Standard remediation banner via the unified _mark_target_connection_error path.
        assert "AGENT_UNREACHABLE" in app.error_text
        assert "target unreachable" in app.error_text
        assert app.target_connection_state == "unreachable"
        # Badge restored despite the failure (exception-safe restore).
        assert not badge.has_class("status--pulse")
        assert status_label.content.plain == "IDLE"


@pytest.mark.asyncio
async def test_with_agent_busy_propagates_non_target_errors(
    config_dir: Path,
) -> None:
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()
        badge = app.query_one("#status-badge")

        async def boom_rpc() -> dict[str, str]:
            raise RuntimeError("boom")

        # Non-TargetCallError bugs must not be masked by the busy convention.
        with pytest.raises(RuntimeError, match="boom"):
            await app._with_agent_busy("loading models…", boom_rpc())

        # Badge still restored even though the error propagated.
        assert not badge.has_class("status--pulse")
        assert badge.has_class("status--idle")


@pytest.mark.asyncio
async def test_model_manager_open_shows_busy_verb_then_opens(
    config_dir: Path,
) -> None:
    class GatedModelClient:
        def __init__(self, gate: asyncio.Event) -> None:
            self.connected = False
            self._gate = gate

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_models":
                await self._gate.wait()
                return {
                    "models": [],
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

    gate = asyncio.Event()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=GatedModelClient(gate),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        # Mid-flight: list_models is parked on the gate, so the busy verb holds.
        await _wait_for_condition(
            lambda: app.query_one("#status-badge").has_class("status--pulse")
            and app.query_one("#status-label", Static).content.plain == "loading models…",
            "busy verb did not appear while list_models was gated",
        )
        assert app.screen.id != "model-manager"

        gate.set()
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not open after list_models released",
        )
        # Badge restored to the live phase once the RPC completed.
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_model_manager_open_banner_keeps_dashboard_when_list_models_fails(
    config_dir: Path,
) -> None:
    class FailingModelClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_models":
                raise TargetCallError("agent-unreachable", "target unreachable")
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("model manager should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=FailingModelClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("m")
        # The busy helper routes the TargetCallError through the unified banner.
        await _wait_for_condition(
            lambda: "AGENT_UNREACHABLE" in app.error_text,
            "unified remediation banner was not rendered on list_models failure",
        )
        # Stayed on the dashboard; the manager never opened.
        assert app.screen.id == "_default"
        assert "target unreachable" in app.error_text


class _GatedVerbClient:
    """Fake target client that parks ONE method on an asyncio.Event.

    Phase-3 verb-badge tests hold the gate open to assert the pulsing busy
    verb deterministically (no sleeps), then release it and let the flow
    finish against the canned ``responses``.
    """

    def __init__(
        self,
        gate: asyncio.Event,
        gated_method: str,
        responses: dict[str, object] | None = None,
    ) -> None:
        self.connected = False
        self._gate = gate
        self._gated_method = gated_method
        self._responses = dict(responses or {})
        self.calls: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def call(self, method: str, params):
        self.calls.append(method)
        if method == self._gated_method:
            await self._gate.wait()
        if method in self._responses:
            return self._responses[method]
        if method == "list_configs":
            return {"valid": [], "invalid": []}
        if method in {"gpu", "sample_gpus"}:
            return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
        if method == "discover_runs":
            return {"runs": []}
        optional = _optional_wizard_section_result(method)
        if optional is not None:
            return optional
        raise AssertionError(f"unexpected target client call: {method}")

    def subscribe(self, *_args, **_kwargs):
        raise AssertionError("verb badge tests should not subscribe")


async def _assert_busy_verb_during(app, pilot, gate: asyncio.Event, verb: str, coro) -> None:
    """Drive ``coro`` and assert the badge pulses with ``verb`` mid-flight."""
    task = asyncio.ensure_future(coro)
    await _wait_for_condition(
        lambda: app.query_one("#status-badge").has_class("status--pulse")
        and app.query_one("#status-label", Static).content.plain == verb,
        f"busy verb {verb!r} did not appear while the RPC was gated",
    )
    gate.set()
    await task
    await pilot.pause()
    assert not app.query_one("#status-badge").has_class("status--pulse")


def _gated_verb_app(config_dir: Path, client: _GatedVerbClient) -> VelaApp:
    return VelaApp(
        configs_dir=config_dir,
        target_client=client,
        target_ping_interval_seconds=None,
    )


@pytest.mark.asyncio
async def test_compose_review_shows_composing_busy_verb(config_dir: Path) -> None:
    gate = asyncio.Event()
    client = _GatedVerbClient(
        gate,
        "compose_config",
        responses={
            "compose_config": {"config": {"name": "demo", "model": "org/m"}},
            "validate_config": {"ok": True},
            "preview": {"preview": "vllm serve org/m", "warnings": []},
        },
    )
    app = _gated_verb_app(config_dir, client)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _assert_busy_verb_during(
            app,
            pilot,
            gate,
            "composing…",
            app._review_new_deployment({"name": "demo", "model_source": "bare"}),
        )
        assert app.screen.id == "new-deployment-review"


@pytest.mark.asyncio
async def test_customize_review_shows_composing_busy_verb(config_dir: Path) -> None:
    # A1 (bug-237): the wizard flag-customize validate+preview path had banners
    # but no busy badge. It must paint the same "composing…" verb the plain
    # compose/review path does, scoped to the RPCs (no nesting).
    gate = asyncio.Event()
    client = _GatedVerbClient(
        gate,
        "validate_config",
        responses={
            "validate_config": {"ok": True},
            "preview": {"preview": "vllm serve org/m", "warnings": []},
        },
    )
    app = _gated_verb_app(config_dir, client)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _assert_busy_verb_during(
            app,
            pilot,
            gate,
            "composing…",
            app._review_customized_new_deployment(
                {"name": "demo", "model": "org/m"},
                {"action": "customize", "engine": {}, "extra_args": []},
                derived=[],
                warnings=[],
            ),
        )
        assert app.screen.id == "new-deployment-review"


@pytest.mark.asyncio
async def test_review_save_shows_saving_busy_verb(config_dir: Path) -> None:
    gate = asyncio.Event()
    client = _GatedVerbClient(
        gate,
        "preflight",
        responses={
            "preflight": {"ok": True},
            "save_config": {
                "name": "demo",
                "config": {"name": "demo", "model": "org/m"},
            },
            "preview": {"preview": "vllm serve org/m", "warnings": []},
        },
    )
    app = _gated_verb_app(config_dir, client)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _assert_busy_verb_during(
            app,
            pilot,
            gate,
            "saving…",
            app._save_reviewed_new_deployment({"name": "demo", "model": "org/m"}),
        )


@pytest.mark.asyncio
async def test_adopt_build_shows_adopting_busy_verb(config_dir: Path) -> None:
    gate = asyncio.Event()
    client = _GatedVerbClient(
        gate,
        "adopt_build",
        responses={
            "adopt_build": {"build_id": "b1", "label": "adopted"},
            "list_builds": {"builds": [], "skipped": []},
        },
    )
    app = _gated_verb_app(config_dir, client)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _assert_busy_verb_during(
            app,
            pilot,
            gate,
            "adopting build…",
            app._adopt_build({"venv": "/tmp/venv", "label": "adopted"}),
        )


@pytest.mark.asyncio
async def test_wizard_pin_model_shows_pinning_busy_verb(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    client = _GatedVerbClient(
        gate,
        "pin_model",
        responses={
            "pin_model": {
                "entry": {"entry_id": "org/m@main", "display_name": "m"},
                "warnings": [],
            },
        },
    )
    app = _gated_verb_app(config_dir, client)
    resumed: list[dict] = []

    async def fake_resume(draft, entry, params, *, warnings):
        resumed.append(dict(entry))

    monkeypatch.setattr(app, "_resume_new_deployment_with_model", fake_resume)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _assert_busy_verb_during(
            app,
            pilot,
            gate,
            "pinning model…",
            app._pin_model_for_new_deployment({"repo_id": "org/m"}, {}),
        )
        assert resumed  # the wizard handoff still resumes after the pin


@pytest.mark.asyncio
async def test_flag_save_shows_saving_flags_busy_verb(config_dir: Path) -> None:
    gate = asyncio.Event()
    client = _GatedVerbClient(
        gate,
        "update_config_flags",
        responses={
            "update_config_flags": {
                "config": {"name": "flags", "model": "org/m"},
            },
            "preview": {"preview": "vllm serve org/m", "warnings": []},
        },
    )
    app = _gated_verb_app(config_dir, client)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _assert_busy_verb_during(
            app,
            pilot,
            gate,
            "saving flags…",
            app._save_flag_manager_changes(
                {"action": "save_flags", "name": "flags", "engine": {}, "extra_args": []}
            ),
        )


@pytest.mark.asyncio
async def test_cancel_job_shows_cancelling_busy_verb(config_dir: Path) -> None:
    gate = asyncio.Event()
    client = _GatedVerbClient(gate, "cancel_job", responses={"cancel_job": {}})
    app = _gated_verb_app(config_dir, client)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _assert_busy_verb_during(
            app,
            pilot,
            gate,
            "cancelling…",
            app._cancel_target_job("job-1"),
        )


@pytest.mark.asyncio
async def test_create_build_form_uv_probe_shows_probing_busy_verb(
    config_dir: Path,
) -> None:
    gate = asyncio.Event()
    client = _GatedVerbClient(
        gate,
        "check_build_prerequisites",
        responses={"check_build_prerequisites": {"uv_available": True}},
    )
    app = _gated_verb_app(config_dir, client)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _assert_busy_verb_during(
            app,
            pilot,
            gate,
            "probing uv…",
            app._open_create_build_form(),
        )
        await pilot.pause()
        assert app.screen.id == "create-build"
        # Badge not stuck pulsing after the failed RPC.
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_build_manager_open_shows_busy_verb_then_opens(
    config_dir: Path,
) -> None:
    gate = asyncio.Event()

    class GatedBuildClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_builds":
                await gate.wait()
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
            raise AssertionError("build manager should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=GatedBuildClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        # Mid-flight: list_builds is parked on the gate, so the busy verb holds.
        await _wait_for_condition(
            lambda: app.query_one("#status-badge").has_class("status--pulse")
            and app.query_one("#status-label", Static).content.plain == "loading builds…",
            "busy verb did not appear while list_builds was gated",
        )
        assert app.screen.id != "build-manager"

        gate.set()
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not open after list_builds released",
        )
        # Badge restored to the live phase once the RPC completed.
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_build_manager_open_banner_keeps_dashboard_when_list_builds_fails(
    config_dir: Path,
) -> None:
    class FailingBuildClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_builds":
                raise TargetCallError("agent-unreachable", "target unreachable")
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("build manager should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=FailingBuildClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        # The busy helper routes the TargetCallError through the unified banner.
        await _wait_for_condition(
            lambda: "AGENT_UNREACHABLE" in app.error_text,
            "unified remediation banner was not rendered on list_builds failure",
        )
        # Stayed on the dashboard; the manager never opened.
        assert app.screen.id == "_default"
        assert "target unreachable" in app.error_text
        # Badge not stuck pulsing after the failed RPC.
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_flag_manager_open_shows_busy_verb_loading_flags(
    config_dir: Path,
) -> None:
    # The flag manager runs TWO RPCs behind ONE overlay: a config preview then
    # list_presets. Gating list_presets holds the overlay so the busy verb is
    # observable while the compound loader runs.
    gate = asyncio.Event()

    class GatedFlagClient:
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
                            "path": "/agent/configs/flags.yaml",
                            "name": "flags",
                            "model": "org/model",
                            "warnings": [],
                            "config": {"name": "flags", "model": "org/model"},
                        },
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "vllm serve org/model", "warnings": [], "metadata": {}}
            if method == "list_presets":
                await gate.wait()
                return {"presets": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("flag manager should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=GatedFlagClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await _wait_for_condition(
            lambda: app.current_config is not None,
            "config was not auto-selected on mount",
        )
        await pilot.press("F")
        # Mid-flight: list_presets is parked on the gate, so the busy verb holds.
        # The textual variant retries if a status widget query transiently raises.
        await _wait_for_textual_condition(
            pilot,
            lambda: app.query_one("#status-badge").has_class("status--pulse")
            and app.query_one("#status-label", Static).content.plain == "loading flags…",
            "busy verb did not appear while list_presets was gated",
        )
        assert app.screen.id != "flag-manager"

        gate.set()
        # The flag manager's #flag-manager-preset Select composes its
        # SelectCurrent/SelectOverlay children AFTER the screen id registers; gate
        # on the Select being fully composed so a mid-mount teardown cannot raise
        # NoMatches: SelectOverlay (bug-209/248 class).
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "flag-manager"
            and bool(app.screen.query("#flag-manager-preset SelectCurrent #label")),
            "flag manager did not open after list_presets released",
        )
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_verify_model_shows_busy_verb_then_restores(
    config_dir: Path,
) -> None:
    gate = asyncio.Event()

    class GatedVerifyClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "verify_model":
                await gate.wait()
                return {"cache_state": "cached", "detail": "12 files"}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("verify model should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=GatedVerifyClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        task = asyncio.ensure_future(app._verify_model("org/model"))
        # Mid-flight: verify_model is parked on the gate, so the busy verb holds.
        await _wait_for_condition(
            lambda: app.query_one("#status-badge").has_class("status--pulse")
            and app.query_one("#status-label", Static).content.plain == "verifying model…",
            "busy verb did not appear while verify_model was gated",
        )

        gate.set()
        await task
        await pilot.pause()
        # Badge restored to the live (idle) phase after success.
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_refresh_models_shows_busy_verb_refreshing_models(
    config_dir: Path,
) -> None:
    # A5(ii): the model-manager Refresh verb was the one RPC 3.2 missed — it did
    # bespoke error handling instead of funnelling through _with_agent_busy, so it
    # never painted the busy badge. It must show "refreshing models…" while the
    # refresh_models RPC runs, then restore the live phase.
    gate = asyncio.Event()

    class GatedRefreshClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "refresh_models":
                await gate.wait()
                return {"refreshed": 3, "models": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("refresh models should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=GatedRefreshClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        task = asyncio.ensure_future(app._refresh_models())
        # Mid-flight: refresh_models is parked on the gate, so the busy verb holds.
        await _wait_for_condition(
            lambda: app.query_one("#status-badge").has_class("status--pulse")
            and app.query_one("#status-label", Static).content.plain
            == "refreshing models…",
            "busy verb did not appear while refresh_models was gated",
        )
        gate.set()
        await task
        await pilot.pause()
        # Badge restored to the live (idle) phase after success.
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_verify_model_banner_on_failure_keeps_state_sane(
    config_dir: Path,
) -> None:
    class FailingVerifyClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "verify_model":
                raise TargetCallError("agent-unreachable", "target unreachable")
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("verify model should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=FailingVerifyClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await app._verify_model("org/model")
        await pilot.pause()
        # Unified remediation banner; no manager was reopened; app stays alive.
        assert "AGENT_UNREACHABLE" in app.error_text
        assert "target unreachable" in app.error_text
        assert app.screen.id == "_default"
        assert app.is_running
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_verify_build_banner_on_failure_keeps_state_sane(
    config_dir: Path,
) -> None:
    # Symmetry with test_verify_model_banner_on_failure_keeps_state_sane (bug-254):
    # a failed verify VERB banners-and-aborts and never reopens the manager, so the
    # two verbs behave identically on the failure path (neither reopens after a
    # TargetCallError; both stay on the dashboard). This is the inverse of
    # test_verify_build_reopens_manager_focused, so it counts list_builds to catch a
    # *deferred* reopen too (the realistic regression: adding _reopen_manager_later to
    # the failure branch). list_builds is only issued when the manager (re)opens.
    class FailingVerifyClient:
        connected = False

        def __init__(self) -> None:
            self.list_builds_calls = 0

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_builds":
                self.list_builds_calls += 1
                return {
                    "builds": [
                        {"build_id": "01STABLE", "label": "stable-cu124",
                         "status": "ready", "default": True},
                    ],
                    "skipped": [],
                }
            if method == "verify_build":
                raise TargetCallError("agent-unreachable", "target unreachable")
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("verify build should not subscribe")

    client = FailingVerifyClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await app._verify_build("stable-cu124")
        # Give any (buggy) deferred _reopen_manager_later a fair chance to fire: the
        # reopen defers 0.05s via set_timer, so 0.2s is a comfortable margin.
        await pilot.pause(0.2)
        # Unified remediation banner; app stays alive.
        assert "AGENT_UNREACHABLE" in app.error_text
        assert "target unreachable" in app.error_text
        assert app.is_running
        assert not app.query_one("#status-badge").has_class("status--pulse")
        # The symmetric failure contract: the manager never (re)opened — still on the
        # dashboard and list_builds was never issued (not even by a deferred reopen).
        assert app.screen.id == "_default"
        assert client.list_builds_calls == 0


class _NewDeploymentSectionsClient:
    """Fake serving all four new-deployment RPCs; individual sections can fail."""

    def __init__(self, *, builds_gate=None, fail=frozenset()):
        self.connected = False
        self._builds_gate = builds_gate
        self._fail = fail

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def call(self, method: str, params):
        if method == "list_configs":
            return {"valid": [], "invalid": []}
        if method == "list_presets":
            if "list_presets" in self._fail:
                raise TargetCallError("agent-unreachable", "target unreachable")
            return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
        if method == "list_deployment_recipes":
            if "list_deployment_recipes" in self._fail:
                raise TargetCallError("agent-unreachable", "target unreachable")
            return {"recipes": [{"name": "recipe-a", "target": "blackbird"}]}
        if method == "list_models":
            if "list_models" in self._fail:
                raise TargetCallError("agent-unreachable", "target unreachable")
            return {
                "models": [
                    {"entry_id": "org/m", "display_name": "org/m", "pinned": True},
                ],
                "default_cache": "hf",
                "app_download_dir": None,
                "skipped": [],
            }
        if method == "list_builds":
            if self._builds_gate is not None:
                await self._builds_gate.wait()
            if "list_builds" in self._fail:
                raise TargetCallError("agent-unreachable", "target unreachable")
            return {"default_build_id": None, "builds": [], "skipped": []}
        if method in {"gpu", "sample_gpus"}:
            return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
        if method == "discover_runs":
            return {"runs": []}
        raise AssertionError(f"unexpected target client call: {method}")

    def subscribe(self, *_args, **_kwargs):
        raise AssertionError("new deployment open should not subscribe")


@pytest.mark.asyncio
async def test_new_deployment_open_shows_busy_verb_spanning_four_rpcs(
    config_dir: Path,
) -> None:
    gate = asyncio.Event()
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=_NewDeploymentSectionsClient(builds_gate=gate),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("n")
        # Mid-flight: the final RPC (list_builds) is gated, so the ONE overlay
        # spanning all four RPCs still holds.
        await _wait_for_condition(
            lambda: app.query_one("#status-badge").has_class("status--pulse")
            and app.query_one("#status-label", Static).content.plain
            == "loading deployment options…",
            "busy verb did not span the four-RPC new-deployment load",
        )
        assert app.screen.id != "new-deployment"

        gate.set()
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "new-deployment",
            "new deployment wizard did not open after list_builds released",
        )
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_new_deployment_open_surfaces_warning_when_only_builds_fails(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=_NewDeploymentSectionsClient(fail=frozenset({"list_builds"})),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        notifications: list[tuple[str, object]] = []
        monkeypatch.setattr(
            app,
            "notify",
            lambda message, **kwargs: notifications.append(
                (str(message), kwargs.get("severity"))
            ),
        )
        await pilot.press("n")
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "new-deployment"
            and bool(app.screen.query("#new-deployment-build-warning")),
            "new deployment wizard did not open with a build warning row",
        )
        # The failed section shows a visible warning row with the code.
        warning = app.screen.query_one("#new-deployment-build-warning", Static)
        assert warning.display is True
        assert "builds unavailable: agent-unreachable" in str(warning.content)
        # A notify warning fired for the failed section.
        assert ("builds unavailable: agent-unreachable", "warning") in notifications
        # The sections that loaded fine still populated their data.
        screen = app.screen
        assert len(screen.recipes) > 0
        assert len(screen.models) > 0
        assert screen.builds == []
        # Sections that loaded fine keep their rows hidden.
        assert screen.query_one("#new-deployment-recipe-warning", Static).display is False
        assert screen.query_one("#new-deployment-model-warning", Static).display is False


@pytest.mark.asyncio
async def test_new_deployment_open_aborts_on_presets_failure(
    config_dir: Path,
) -> None:
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=_NewDeploymentSectionsClient(fail=frozenset({"list_presets"})),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("n")
        # presets is the required first RPC: its failure aborts the open via the
        # unified remediation banner, and the wizard never opens.
        await _wait_for_condition(
            lambda: "AGENT_UNREACHABLE" in app.error_text,
            "unified remediation banner was not rendered on list_presets failure",
        )
        assert app.screen.id == "_default"
        assert "target unreachable" in app.error_text
        assert not app.query_one("#status-badge").has_class("status--pulse")


@pytest.mark.asyncio
async def test_new_deployment_section_failure_records_debug_breadcrumb(
    config_dir: Path, tmp_path: Path
) -> None:
    # A2: a swallowed optional-section RPC failure degrades the wizard to a
    # warning-row layout. Leave a debug breadcrumb (mirroring
    # detached.discovery_failed) so that silent degradation is diagnosable.
    debug_log_path = tmp_path / "debug.jsonl"

    class SectionFailingClient:
        def __init__(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {"presets": [{"name": "balanced", "description": "", "engine": {}}]}
            if method == "list_deployment_recipes":
                return {"recipes": []}
            if method == "list_models":
                raise TargetCallError("agent-unreachable", "models rpc down")
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("new deployment section test should not subscribe")

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=SectionFailingClient(),
        target_ping_interval_seconds=None,
        debug_log_path=debug_log_path,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("n")
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "new-deployment"
            and bool(app.screen.query("#new-deployment-model-warning")),
            "new deployment wizard did not open with the models warning row",
        )

    records = [
        json.loads(line)
        for line in debug_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record["event"] == "new_deployment.section_failed"
        and record["payload"]["section"] == "models"
        and "models rpc down" in record["payload"]["error"]
        for record in records
    )


@pytest.mark.asyncio
async def test_target_switch_shows_connecting_verb_then_restores(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="user@gpu-host",
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

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                # Gate ONLY the switched-to target so the connecting overlay holds.
                if self.target.name == "blackbird":
                    await gate.wait()
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("target selection should not subscribe")

    monkeypatch.setattr(
        tui_app_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda target, **_kwargs: FactoryTargetClient(target),
    )

    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("t")
        await pilot.press("down")
        await pilot.press("enter")
        # Mid-switch: blackbird's registry load is gated, so the connecting verb holds.
        await _wait_for_condition(
            lambda: app.query_one("#status-badge").has_class("status--pulse")
            and app.query_one("#status-label", Static).content.plain
            == "connecting to blackbird…",
            "connecting verb did not appear during the target switch",
        )

        gate.set()
        # target_name flips to blackbird early in the switch, so wait on the busy
        # overlay CLEARING (its finally) rather than on the state var.
        await _wait_for_condition(
            lambda: not app.query_one("#status-badge").has_class("status--pulse"),
            "connecting verb did not clear after the registry load released",
        )
        assert app.target_name == "blackbird"


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
    app = VelaApp(configs_dir=config_dir)

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

        assert _static_text(app, "#app-title") == "Vela"
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

        status = app.query_one("#status-badge")
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

    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=PreviewMetadataTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.pause()

        segment = _static_text(app, "#active-model")
        assert "build: 📌nightly-cu130 ●" in segment
        assert "model: 📌llama-pin ● abc123" in segment
        assert "Target: blackbird" in app.config_summary
        assert "Build: 📌nightly-cu130 ●" in app.config_summary
        assert "Model state: 📌llama-pin ● abc123" in app.config_summary


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
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.pause()
        assert "build: stable-cu124 ●" in _static_text(app, "#active-model")

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
            and "build: nightly-cu130 ●" in _static_text(app, "#active-model"),
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

    app = VelaApp(
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
                            "live_refs": [{"run_id": "run-live"}],
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

    app = VelaApp(
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
    app = VelaApp(
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
        assert "uv available on blackbird" in uv_note
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
                "method": "pip",
            },
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
    app = VelaApp(
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
        assert "vela build doctor --target blackbird" in app.error_text


@pytest.mark.asyncio
async def test_build_manager_keeps_create_form_open_on_uv_precheck_failure(
    config_dir: Path,
) -> None:
    class BuildTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []
            self.precheck_attempted = asyncio.Event()

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
                self.precheck_attempted.set()
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
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        await pilot.press("n")
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "create-build",
            "create build screen did not open",
        )
        app.screen.query_one("#create-build-method", Select).value = "nightly"
        app.screen.query_one("#create-build-label", Input).value = "nvfp4"
        app.screen.query_one("#create-build-channel", Input).value = "cu130"
        await pilot.press("enter")

        await asyncio.wait_for(target_client.precheck_attempted.wait(), timeout=5)
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "create-build"
            and "requires uv" in str(
                app.screen.query_one("#create-build-error", Static).content
            ),
            "create build form did not reopen with uv precheck error",
        )

        assert app.screen.query_one("#create-build-method", Select).value == "nightly"
        assert app.screen.query_one("#create-build-label", Input).value == "nvfp4"
        assert app.screen.query_one("#create-build-channel", Input).value == "cu130"
        assert all(method != "create_build" for method, _params in target_client.calls)
        assert all(run_ids == ["__agent__"] for run_ids, _ in target_client.subscribe_calls)


@pytest.mark.asyncio
async def test_build_manager_blocks_uv_only_method_in_create_form_before_dispatch(
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
                return {"ok": True, "method": params["method"], "uv_available": False}
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
    app = VelaApp(
        configs_dir=config_dir,
        target_name="blackbird",
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test(size=(144, 45)) as pilot:
        await _wait_for_target_connection_state(app, "connected")
        await pilot.press("b")
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "build-manager",
            "build manager did not open",
        )
        await pilot.press("n")
        await _wait_for_textual_condition(
            pilot,
            lambda: app.screen.id == "create-build"
            and "uv not found"
            in str(app.screen.query_one("#create-build-uv-note", Static).content),
            "create build screen did not show target uv status",
        )

        app.screen.query_one("#create-build-method", Select).value = "nightly"
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.id == "create-build"
        assert "requires uv" in str(app.screen.query_one("#create-build-error", Static).content)
        assert all(method != "create_build" for method, _params in target_client.calls)
        assert [
            params
            for method, params in target_client.calls
            if method == "check_build_prerequisites"
        ] == [{"method": "pip"}]
        assert all(run_ids == ["__agent__"] for run_ids, _ in target_client.subscribe_calls)


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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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

    app = VelaApp(
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
        assert "> ● llama-pin  hf  cached  2.1 GB  abc123" in model_list
        assert "  ○ qwen-remote  hf  remote_only  —  main" in model_list
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
    app = VelaApp(
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
        assert "model: qwen-remote ○ main" in _static_text(app, "#active-model")


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

    app = VelaApp(
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
    app = VelaApp(
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
        assert "> ● qwen-remote  hf  cached  —  main" in model_list


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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(
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
            lambda: app.screen.id == "pin-model" and bool(app.screen.query(Input)),
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
    app = VelaApp(
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
            lambda: app.screen.id == "pin-model" and bool(app.screen.query(Input)),
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
    app = VelaApp(
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
    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)
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
    app = VelaApp(configs_dir=config_dir)

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

        status_content = app.query_one("#status-label", Static).content
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
    app = VelaApp(configs_dir=config_dir)

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
async def test_configs_title_does_not_duplicate_selected_line(config_dir: Path) -> None:
    write_yaml(config_dir / "solo.yaml", "name: solo\nmodel: org/solo-model")
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_config("solo")
        await pilot.pause()

        title = app.query_one("#configs-title", Static).content
        body = app.query_one("#configs", Static).content
        assert isinstance(title, Text)
        assert isinstance(body, Text)
        assert "Selected: solo" in body.plain
        assert "Selected:" not in title.plain


@pytest.mark.asyncio
async def test_configs_card_reports_target_unreachable_when_disconnected_and_empty(
    config_dir: Path,
) -> None:
    # bug-252 (bullet 1): an unreachable target with no cached configs must NOT
    # render the first-run "No configs yet" empty state — that reads as "your
    # configs were deleted". The card must state the target is unreachable and
    # offer reconnect.
    class UnreachableTargetClient:
        connected = False

        async def connect(self):
            self.connected = True
            return None

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                raise TargetCallError("agent-unreachable", "target unreachable")
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            return {}

        def subscribe(self, *_args, **_kwargs):
            raise RuntimeError("no stream in unreachable test")

    app = VelaApp(
        configs_dir=config_dir,
        target_client=UnreachableTargetClient(),
        target_ping_interval_seconds=None,
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        # The registry genuinely failed to load (stays empty), so the card renders
        # from the offline state. Assert on the CARD, not target_connection_state —
        # a post-mount gpu worker can reset the state var, but it does not re-render
        # the configs card (the card was rendered during on_mount while offline).
        body = app.query_one("#configs", Static).content
        title = app.query_one("#configs-title", Static).content
        assert isinstance(body, Text)
        assert isinstance(title, Text)
        assert "target unreachable — configs unknown · R reconnect" in body.plain
        assert "No configs yet" not in body.plain
        assert _text_uses_style(body, tui_app_module.WARN)
        # The (unknown) count line is replaced by an honest connection marker.
        assert "target unreachable" in title.plain


@pytest.mark.asyncio
async def test_configs_card_keeps_first_run_copy_when_connected_and_empty(
    config_dir: Path,
) -> None:
    # bug-252 guard (bullet 2): a genuinely-connected target with an empty
    # registry keeps the first-run onboarding copy UNCHANGED — only the offline
    # state gets the new honesty treatment.
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert app.target_connection_state == "connected"
        body = app.query_one("#configs", Static).content
        title = app.query_one("#configs-title", Static).content
        assert isinstance(body, Text)
        assert (
            "No configs yet — press n to create your first deployment · ? help"
            in body.plain
        )
        assert "target unreachable" not in body.plain
        assert "target unreachable" not in title.plain


@pytest.mark.asyncio
async def test_configs_card_keeps_cached_entries_and_flags_unreachable_when_disconnected(
    config_dir: Path,
) -> None:
    # bug-252 (bullet 3): a drop AFTER configs were cached must keep showing the
    # cached entries (they are not gone) but replace the confident count line with
    # an honest "target unreachable" marker.
    write_yaml(config_dir / "solo.yaml", "name: solo\nmodel: org/solo-model")
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Connected baseline: entry visible, confident count line present.
        assert app.target_connection_state == "connected"
        baseline_title = app.query_one("#configs-title", Static).content
        assert "1 valid" in baseline_title.plain

        # Simulate the link dropping after configs were cached, then re-render the
        # card. Synchronous set + render + read (no await) so nothing races.
        app.target_connection_state = "unreachable"
        app._refresh_target_backed_views()

        body = app.query_one("#configs", Static).content
        title = app.query_one("#configs-title", Static).content
        assert isinstance(body, Text)
        assert isinstance(title, Text)
        # Cached entries are still shown — the configs are not gone.
        assert "solo" in body.plain
        # ...but the count line is replaced by the honest unreachable marker.
        assert "target unreachable" in title.plain
        assert "1 valid" not in title.plain
        assert _text_uses_style(title, tui_app_module.WARN)
        # The body did NOT fall back to the empty "configs unknown" copy.
        assert "configs unknown" not in body.plain


@pytest.mark.asyncio
async def test_mark_target_disconnected_renders_offline_card_immediately(
    config_dir: Path,
) -> None:
    # bug-253: a mid-session drop detected by keepalive must re-render the Configs
    # card at once. _mark_target_disconnected only refreshed chrome (which does not
    # touch #configs), so the card kept showing the stale connected copy until the
    # next manual refresh.
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Connected + empty baseline: first-run onboarding copy.
        assert app.target_connection_state == "connected"
        body = app.query_one("#configs", Static).content
        assert "No configs yet" in body.plain

        await app._mark_target_disconnected("link down")

        body = app.query_one("#configs", Static).content
        assert isinstance(body, Text)
        assert "target unreachable — configs unknown · R reconnect" in body.plain
        assert "No configs yet" not in body.plain


@pytest.mark.asyncio
async def test_mark_target_connection_error_renders_offline_card_immediately(
    config_dir: Path,
) -> None:
    # bug-253 sibling: a TargetCallError surfaced by _mark_target_connection_error
    # must also re-render the Configs card so the offline state is visible at once.
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.target_connection_state == "connected"
        body = app.query_one("#configs", Static).content
        assert "No configs yet" in body.plain

        app._mark_target_connection_error(
            TargetCallError("agent-unreachable", "target unreachable")
        )

        body = app.query_one("#configs", Static).content
        assert isinstance(body, Text)
        assert "target unreachable — configs unknown · R reconnect" in body.plain


@pytest.mark.asyncio
async def test_reconnect_restores_normal_configs_card(config_dir: Path) -> None:
    # bug-252 (bullet 4): pressing R to reconnect a restored target must reload the
    # registry and re-render the normal card, not leave the "target unreachable"
    # copy frozen on screen.
    class FlakyTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.reachable = False

        async def connect(self):
            self.connected = True
            return None

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                if not self.reachable:
                    raise TargetCallError("agent-unreachable", "target unreachable")
                return {
                    "valid": [
                        {
                            "path": "/tmp/solo.yaml",
                            "config": {"name": "solo", "model": "org/solo-model"},
                            "warnings": [],
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "solo preview", "metadata": {}}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            return {}

        def subscribe(self, *_args, **_kwargs):
            raise RuntimeError("no stream in flaky test")

    target_client = FlakyTargetClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_client=target_client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        # Disconnected + empty on mount → the honest unreachable card.
        body = app.query_one("#configs", Static).content
        assert "target unreachable — configs unknown · R reconnect" in body.plain

        # Target comes back; the operator presses R (action_reconnect).
        target_client.reachable = True
        app.action_reconnect()

        await _wait_for_condition(
            lambda: "solo" in str(app.query_one("#configs", Static).content),
            "reconnect did not restore the normal configs card",
        )
        body = app.query_one("#configs", Static).content
        title = app.query_one("#configs-title", Static).content
        assert "solo" in body.plain
        assert "target unreachable" not in body.plain
        assert "target unreachable" not in title.plain
        assert "1 valid" in title.plain


@pytest.mark.asyncio
async def test_figma_dashboard_pills_and_selection_use_surface_styles(
    config_dir: Path,
) -> None:
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
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_phase(Phase.LOADING_WEIGHTS)

        configs_title = app.query_one("#configs-title", Static).content
        configs = app.query_one("#configs", Static).content
        status = app.query_one("#status-label", Static).content
        controls = app.query_one("#log-controls", Static).content

        assert isinstance(configs_title, Text)
        assert isinstance(configs, Text)
        assert isinstance(status, Text)
        assert isinstance(controls, Text)
        assert _text_uses_style(configs_title, "on #0e2a21")
        assert _text_uses_style(configs_title, "on #2b2410")
        assert _text_uses_style(configs, "on #0c2238")
        assert _text_uses_style(configs, "on #2b2410")
        assert _text_uses_style(status, "on #2b2410")
        assert _text_uses_style(controls, "on #0e2a21")
        assert _text_uses_style(controls, "on #14202b")


@pytest.mark.asyncio
async def test_terminal_phases_clear_stale_progress_line(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._update_progress("Loading safetensors checkpoint shards: 100% 4/4")
        assert app.progress_text

        app._set_phase(Phase.READY)

        assert app.progress_text == ""
        assert app.query_one("#progress-line").display is False


@pytest.mark.asyncio
async def test_phase_timeline_ends_on_terminal_marker_row(config_dir: Path) -> None:
    # bug-237: after an operator stop the STOPPED pill coexisted with a
    # timeline still ending on READY ✓. The panel must append a terminal
    # marker row — ■ STOPPED dim, ✗ CRASHED red — and be tall enough to show
    # it together with the Overall line.
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._set_phase(Phase.STARTING)
        app._set_phase(Phase.READY)
        app._set_phase(Phase.STOPPED)

        lines = app.phase_timeline_text.splitlines()
        # READY history is preserved, but the timeline no longer ENDS on it.
        assert "✓ READY" in app.phase_timeline_text
        assert lines[-1].startswith("Overall")
        assert lines[-2].startswith("■ STOPPED")
        # The stepper card fits the full history + terminal row + Overall
        # (content lines + 2 border rows) instead of clipping the tail.
        panel = app.query_one("#phase-panel")
        assert panel.styles.max_height is not None
        assert panel.styles.max_height.value >= len(lines) + 2

        # A crash renders a red ✗ CRASHED terminal row.
        app.fsm.error_kind = ErrorKind.CRASHED
        app._set_phase(Phase.ERROR)
        assert app.phase_timeline_text.splitlines()[-2].startswith("✗ CRASHED")
        phases = app.query_one("#phases", Static).content
        assert isinstance(phases, Text)
        assert _text_uses_style(phases, tui_app_module.BAD)

        # Non-crash errors keep the honest phase name, still marked ✗.
        app.fsm.error_kind = ErrorKind.OOM
        app._set_phase(Phase.STOPPED)
        app._set_phase(Phase.ERROR)
        assert app.phase_timeline_text.splitlines()[-2].startswith("✗ ERROR")


@pytest.mark.asyncio
async def test_progress_record_after_terminal_phase_stays_hidden(config_dir: Path) -> None:
    # bug-237: after READY the transient download panel lingered at "100% 4/4"
    # because a trailing carriage-return record re-showed it after _set_phase's
    # clear. Once the FSM leaves the loading family (READY) or the run ends
    # (STOPPED/ERROR), stale run progress must stay hidden.
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        # During a loading phase the transient panel still renders (FR-10).
        app._set_phase(Phase.DOWNLOADING_MODEL)
        app._update_progress("Loading safetensors checkpoint shards: 50% 2/4")
        assert "50%" in app.progress_text
        assert app.query_one("#progress-panel").display is True

        app._set_phase(Phase.READY)
        assert app.progress_text == ""

        # The trailing transient record must not resurrect the panel.
        app.handle_log_record(
            LogRecord("transient", "Loading safetensors checkpoint shards: 100% 4/4", None)
        )
        await pilot.pause()
        assert app.progress_text == ""
        assert app.query_one("#progress-panel").display is False

        # Terminal states behave the same (STOPPED here; ERROR by the same gate).
        app._set_phase(Phase.STOPPED)
        app._update_progress("Loading safetensors checkpoint shards: 100% 4/4")
        assert app.progress_text == ""
        assert app.query_one("#progress-panel").display is False


@pytest.mark.asyncio
async def test_progress_still_streams_for_active_job_after_ready(config_dir: Path) -> None:
    # A background job started while a server is READY (e.g. pin+download)
    # keeps its progress stream — the terminal gate only drops stale RUN
    # progress, never live job progress.
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_phase(Phase.READY)
        app._active_job_id = "job-1"
        app._update_progress("Downloading model: 10% 1/10")
        assert "10%" in app.progress_text
        assert app.query_one("#progress-panel").display is True


@pytest.mark.asyncio
async def test_progress_panel_hides_when_job_ends_while_ready(config_dir: Path) -> None:
    # A2 (bug-237): a background job that finishes while the server is READY
    # must not leave its last streamed percent frozen in the transient panel.
    # The job-final "Job complete" ProgressUpdated is pumped AFTER
    # _run_target_job clears _active_job_id, so the READY suppression gate drops
    # it — the panel must be cleared on job end instead of retaining the stale
    # percent.
    class _GatedJobEvents:
        def __init__(self, job_id: str) -> None:
            self.job_id = job_id
            self.release_done = asyncio.Event()
            self.closed = False
            self._stage = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._stage == 0:
                self._stage = 1
                return {
                    "event": "job_progress",
                    "job_id": self.job_id,
                    "kind": "transient",
                    "text": "Downloading model: 87% 7/8",
                }
            if self._stage == 1:
                self._stage = 2
                await self.release_done.wait()
                return {
                    "event": "job_done",
                    "job_id": self.job_id,
                    "ok": True,
                    "detail": "Job complete",
                }
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    class _IdleEvents:
        # The mount-time `__agent__` subscription must not consume the job's
        # events — park it forever instead.
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            pass

    class JobClient:
        def __init__(self) -> None:
            self.connected = False
            self.events = _GatedJobEvents("job-dl")

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            if method == "download_model":
                return {"job_id": params["job_id"], "status": "running"}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            if "job-dl" in run_ids:
                return self.events
            return _IdleEvents()

    client = JobClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)

    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_phase(Phase.READY)
        task = asyncio.ensure_future(
            app._run_target_job(
                "download_model",
                {"job_id": "job-dl", "model_ref": "org/m"},
                error_action="download model",
                incomplete_label="Model download",
            )
        )
        # The live streamed progress renders while the job runs (active job).
        await _wait_for_condition(
            lambda: "87%" in app.progress_text
            and app.query_one("#progress-panel").display is True,
            "job progress did not render while READY",
        )
        # Finish the job. Its "Job complete" ProgressUpdated is suppressed by the
        # READY gate, so on job end the panel must be cleared — not left frozen.
        client.events.release_done.set()
        await task
        await pilot.pause()
        assert app.progress_text == ""
        assert app.query_one("#progress-panel").display is False


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
    class QuietTargetClient:
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
                            "path": str(config_dir / "messages.yaml"),
                            "name": "messages",
                            "model": "org/model",
                            "target": None,
                            "warnings": [],
                            "config": {
                                "name": "messages",
                                "model": "org/model",
                                "server": {"host": "127.0.0.1", "port": 8126},
                            },
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/model", "warnings": []}
            if method == "discover_runs":
                return {"runs": []}
            if method == "gpu":
                return {"ok": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            if list(run_ids) == ["__agent__"]:
                async def events():
                    while True:
                        await asyncio.sleep(60)
                        yield {}

                return events()
            raise AssertionError("message test should only subscribe to agent GPU events")

    app = VelaApp(
        configs_dir=config_dir,
        gpu_interval_seconds=60,
        target_client=QuietTargetClient(),
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
    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(
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

    app = VelaApp(
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

    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir)
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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("l")
        await _wait_for_phase(app, Phase.ERROR)

        assert app.fsm.error_kind is ErrorKind.CRASHED
        assert "CRASHED" in app.error_text
        assert "synthetic loader abort before ready" in app.error_text


@pytest.mark.asyncio
async def test_nonzero_exit_without_logs_shows_exit_code_excerpt(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

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
    config_dir: Path, tmp_path: Path, unused_tcp_port: int
) -> None:
    missing_executable = tmp_path / "does-not-exist"
    write_yaml(
        config_dir / "missing-bin.yaml",
        f"""
        name: missing-bin
        model: fake/model
        server:
          port: {unused_tcp_port}
        command:
          entrypoint: serve
          executable: {missing_executable}
        """,
    )
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
        app = VelaApp(configs_dir=config_dir)

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
    config_dir: Path, tmp_path: Path, unused_tcp_port: int
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
        server:
          port: {unused_tcp_port}
        launch:
          mode: detached
          runs_dir: {runs_dir}
        """,
    )
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)
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

    app = VelaApp(configs_dir=config_dir)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            reattach = await _wait_for_command(
                app, "Reattach detached run: detached"
            )
            reattach.callback()
            await _wait_for_phase(app, Phase.READY, pilot=pilot)
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
    app = VelaApp(configs_dir=config_dir)
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

    def cancel_group(_app: VelaApp, group: str) -> None:
        cancelled_groups.append(group)

    agent = StopRefusingAgent()
    app = VelaApp(
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

    def cancel_group(_app: VelaApp, group: str) -> None:
        cancelled_groups.append(group)

    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()

        await app._reattach_target_detached_run("run-1")

        assert "Unable to reattach" in app.error_text
        assert "run-1" in app.error_text


def test_tui_does_not_expose_path_based_detached_reattach() -> None:
    assert "reattach_detached_run" not in VelaApp.__dict__


def test_tui_does_not_compute_target_runs_dirs() -> None:
    assert "_runs_dirs" not in VelaApp.__dict__


def test_tui_constructor_only_accepts_target_client_boundary() -> None:
    params = inspect.signature(VelaApp).parameters

    assert "target_client" in params
    assert "target_name" in params
    assert "agent" not in params
    assert "gpu_sampler" not in params


def test_tui_does_not_store_attached_process_handle(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

    assert "current_process" not in app.__dict__


def test_tui_does_not_store_reattached_sidecar_path(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        await app._reattach_target_detached_run("run-1")

        assert worker_names[:2] == ["reattach-tail", "reattach-health"]


@pytest.mark.asyncio
async def test_reattach_tail_worker_is_non_crashing_monitor(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a network blip during the reattach tail RPC must not crash the
    # whole TUI while the detached GPU server keeps running (bug-084 sibling).
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
        return SimpleNamespace()

    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "run_worker", capture_worker)

        await app._reattach_target_detached_run("run-1")

        tail_worker = next(
            call for call in worker_calls if call["name"] == "reattach-tail"
        )
        assert tail_worker["group"] == "tail"
        assert tail_worker.get("exit_on_error") is False


@pytest.mark.asyncio
async def test_load_worker_is_non_crashing_monitor(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a malformed launch payload or a dropped connection during the
    # attached launch/monitor must surface an error, never crash the TUI.
    from vela.config.loader import ConfigRegistry, ValidConfig
    from vela.config.schema import ModelConfig

    worker_calls: list[dict[str, object]] = []

    def capture_worker(coro, **kwargs):
        worker_calls.append(kwargs)
        coro.close()
        return SimpleNamespace()

    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        cfg = ModelConfig(name="alpha", model="org/alpha")
        app.registry = ConfigRegistry(
            valid=[ValidConfig(path=config_dir / "alpha.yaml", config=cfg)]
        )
        app.current_config = cfg
        app.target_connection_state = "connected"
        monkeypatch.setattr(app, "run_worker", capture_worker)

        app.action_load()

        load_worker = next(
            call for call in worker_calls if call.get("name") == "load"
        )
        assert load_worker["group"] == "engine"
        assert load_worker.get("exit_on_error") is False


def test_load_and_tail_worker_groups_surface_failures_as_optional_monitors() -> None:
    # The load ("engine") and detached-tail ("tail") worker groups must be
    # registered so on_worker_state_changed surfaces their errors instead of the
    # failure being silent after exit_on_error=False.
    assert "engine" in tui_app_module.OPTIONAL_MONITOR_GROUP_LABELS
    assert "tail" in tui_app_module.OPTIONAL_MONITOR_GROUP_LABELS


def test_every_run_worker_spawn_passes_exit_on_error_false() -> None:
    # bug-227 class: EVERY run_worker( spawn in app.py must pass
    # exit_on_error=False so an unhandled worker exception surfaces via
    # on_worker_state_changed instead of raising WorkerFailed and killing the
    # whole TUI. Enforced structurally so a future spawn cannot silently
    # regress. (Source-level assertion in the style of test_tui_screen_parsers.)
    source = inspect.getsource(tui_app_module)
    offenders: list[int] = []
    for match in re.finditer(r"run_worker\(", source):
        depth = 1
        index = match.end()
        while index < len(source) and depth:
            char = source[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        call_text = source[match.start() : index]
        if "exit_on_error=False" not in call_text:
            offenders.append(source.count("\n", 0, match.start()) + 1)
    assert offenders == [], (
        f"run_worker spawns missing exit_on_error=False at app.py lines: {offenders}"
    )


def test_lifecycle_worker_groups_are_registered_as_optional_monitors() -> None:
    # The restart, stop/kill (engine-signal), quit, target-switch, and reattach
    # workers run with exit_on_error=False; their groups must be registered so
    # on_worker_state_changed surfaces a failure as a warning instead of it
    # being swallowed silently (bug-227 class).
    for group in ("restart", "engine-signal", "quit", "target-switch", "reattach"):
        assert group in tui_app_module.OPTIONAL_MONITOR_GROUP_LABELS


def test_every_run_worker_group_is_monitored_or_self_reporting() -> None:
    # Task 1.2 carry-forward (Part B): every group= used by a run_worker in
    # app.py must be EITHER registered in OPTIONAL_MONITOR_GROUP_LABELS (so
    # on_worker_state_changed surfaces its failure as a warning) OR listed in
    # SELF_REPORTING_WORKER_GROUPS (its worker broad-guards and reports its own
    # outcome). Crash-proofing (exit_on_error=False) without this leaves failures
    # SILENT. Enforced structurally so every FUTURE group makes the choice
    # consciously instead of regressing to a silent swallow.
    source = inspect.getsource(tui_app_module)
    used_groups: set[str] = set()
    for match in re.finditer(r"run_worker\(", source):
        depth = 1
        index = match.end()
        while index < len(source) and depth:
            char = source[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        call_text = source[match.start() : index]
        group_match = re.search(r'group="([^"]+)"', call_text)
        if group_match is not None:
            used_groups.add(group_match.group(1))
    # Sanity: the scanner actually found the known lifecycle groups.
    assert {"engine", "new-deployment", "build-manager"} <= used_groups
    registered = set(tui_app_module.OPTIONAL_MONITOR_GROUP_LABELS) | set(
        tui_app_module.SELF_REPORTING_WORKER_GROUPS
    )
    unclassified = sorted(used_groups - registered)
    assert unclassified == [], (
        "run_worker groups neither monitored nor self-reporting "
        f"(add to OPTIONAL_MONITOR_GROUP_LABELS or SELF_REPORTING_WORKER_GROUPS): "
        f"{unclassified}"
    )
    # A5(i): a group makes ONE choice, never both. A group in BOTH sets is a
    # contradiction (monitored AND self-reporting) and almost certainly a
    # copy-paste slip — keep the two sets disjoint.
    both = sorted(
        set(tui_app_module.OPTIONAL_MONITOR_GROUP_LABELS)
        & set(tui_app_module.SELF_REPORTING_WORKER_GROUPS)
    )
    assert both == [], (
        "worker groups must be EITHER monitored OR self-reporting, never both: "
        f"{both}"
    )


@pytest.mark.asyncio
async def test_restart_monitor_failure_does_not_crash_app(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-227 class: a restart whose monitor path raises (restart RPC succeeds,
    # then the monitor call raises) must not kill the TUI. The restart worker
    # runs with exit_on_error=False and its group is registered, so the failure
    # surfaces as a warning notification and the app keeps running instead of
    # raising WorkerFailed. _restart_attached_run awaits _monitor_restart_result
    # OUTSIDE its try, so the raise escapes straight to the worker.
    write_yaml(
        config_dir / "alpha.yaml",
        """
        name: alpha
        model: org/alpha
        """,
    )

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
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("restart monitor failure test should not subscribe")

    app = VelaApp(configs_dir=config_dir, target_client=FakeTargetClient())
    notifications: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, *args, **kwargs: notifications.append(
            (str(message), kwargs.get("severity"))
        ),
    )

    async def exploding_monitor(*_args, **_kwargs) -> None:
        raise RuntimeError("monitor exploded")

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_config = load_registry(config_dir).by_name("alpha")
        app.current_run_id = "run-1"
        app.target_connection_state = "connected"
        monkeypatch.setattr(
            app, "_monitor_restart_result", exploding_monitor, raising=False
        )

        app.action_restart()
        await _wait_for_condition(
            lambda: any(sev == "warning" for _msg, sev in notifications)
            or not app.is_running,
            "restart worker failure did not surface as a warning",
        )

        assert app.is_running
        assert any(
            "restart monitor stopped" in msg and sev == "warning"
            for msg, sev in notifications
        )


@pytest.mark.asyncio
async def test_reattach_malformed_payload_missing_run_id_refuses_without_keyerror(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-227 class: a malformed agent reattach payload (missing run_id) must
    # render the same "Unable to reattach" refusal as a corrupt sidecar, not
    # raise a KeyError that (via the reattach worker) could crash the TUI.
    payload = _target_reattach_payload(served_model_names=["fake-model"])
    del payload["run_id"]
    fake_target_client = _fake_reattach_target_client(payload)
    monkeypatch.setattr(
        tui_app_module,
        "target_client_for_config",
        lambda _target, **_kwargs: fake_target_client(),
    )
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()

        await app._reattach_target_detached_run("run-1")

        assert "Unable to reattach" in app.error_text
        assert "run-1" in app.error_text
        assert app.reattached_run_id is None


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
    app = VelaApp(configs_dir=config_dir)
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

    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(configs_dir=config_dir, target_name="blackbird")

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

    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(configs_dir=config_dir)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            await _reattach_discovered_target_run(app, launch.run_id)
            await _wait_for_phase(app, Phase.READY, pilot=pilot)
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

    app = VelaApp(configs_dir=config_dir)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            await _reattach_discovered_target_run(app, launch.run_id)
            await _wait_for_phase(app, Phase.READY, pilot=pilot)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(
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
        (ErrorKind.IMAGE_NOT_FOUND, "No such image", "command.docker.image"),
        (ErrorKind.DAEMON_UNREACHABLE, "Docker daemon unavailable", "daemon socket"),
        (ErrorKind.NAME_CONFLICT, "container name already in use", "command.docker.evict"),
        (ErrorKind.GPU_NOT_AVAILABLE, "could not select device driver", "NVIDIA runtime"),
        (ErrorKind.CRASHED, "INFO Starting to load model", "last log lines"),
    ],
)
async def test_wire_phase_error_shows_named_banner(
    config_dir: Path, kind: ErrorKind, excerpt: str, guidance: str
) -> None:
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.press("l")
        await _wait_for_log(app, "Uvicorn running")
        await _wait_for_phase(app, Phase.READY)
        assert any("Initializing a V1 LLM engine" in line for line in app.log_lines)
        await pilot.press("s")
        await _wait_for_stopped(app)


@pytest.mark.asyncio
async def test_run_separator_and_operator_stop_closure(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-237 live findings: launching again concatenated both runs' logs with
    # no delimiter, and operator stop flipped the pill with no toast/log line.
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
    app = VelaApp(configs_dir=config_dir)
    toasts: list[str] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, *args, **kwargs: toasts.append(str(message)),
    )

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_log(app, "Uvicorn running")
            await _wait_for_phase(app, Phase.READY)
            run_id = app.current_run_id
            assert run_id
            # Each launch opens with a dim display-only separator line that
            # precedes every line the run itself streams.
            separator = f"── run {run_id} · fake · local ──"
            assert separator in app.log_lines
            first_child_line = next(
                index
                for index, line in enumerate(app.log_lines)
                if "Uvicorn running" in line
            )
            assert app.log_lines.index(separator) < first_child_line

            await pilot.press("s")
            await _wait_for_stopped(app)
            # Operator stop closes the loop: a toast AND a display log line.
            assert any(f"Stopped {run_id}" in toast for toast in toasts)
            assert app.log_lines[-1] == "── STOPPED by operator ──"

            # Launching again starts with a fresh separator, so consecutive
            # runs never read as one concatenated stream.
            await pilot.press("l")
            await _wait_for_phase(app, Phase.READY)
            second_run_id = app.current_run_id
            assert second_run_id and second_run_id != run_id
            separators = [
                line for line in app.log_lines if line.startswith("── run ")
            ]
            assert separators == [
                f"── run {run_id} · fake · local ──",
                f"── run {second_run_id} · fake · local ──",
            ]
            await pilot.press("s")
            await _wait_for_stopped(app)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_reattached_stop_signal_notifies_and_logs_closure(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reattached (detached-run) stop path must close the loop the same way
    # the attached path does: toast + display-only operator line.
    app = VelaApp(configs_dir=config_dir)
    toasts: list[str] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, *args, **kwargs: toasts.append(str(message)),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.reattached_run_id = "run-detached-1"

        async def fake_target_call(method: str, params: dict) -> dict:
            assert method == "stop"
            return {}

        monkeypatch.setattr(app, "_target_call", fake_target_call)
        await app._signal_reattached_target_run("stop")
        await pilot.pause()

        assert app.phase is Phase.STOPPED
        assert any("Stopped run-detached-1" in toast for toast in toasts)
        assert app.log_lines[-1] == "── STOPPED by operator ──"


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
    app = VelaApp(configs_dir=config_dir)
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
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
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
    app = VelaApp(configs_dir=config_dir)
    toasts: list[str] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, *args, **kwargs: toasts.append(str(message)),
    )

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_phase(app, Phase.READY)
            run_id = app.current_run_id
            assert run_id

            await pilot.press("K")
            await pilot.press("enter")
            await _wait_for_stopped(app)
            await _wait_for_phase(app, Phase.STOPPED)

            assert app.fsm.error_kind is None
            assert "CRASHED" not in app.error_text
            # Operator kill closes the loop too (bug-237): toast + display line.
            assert any(f"Killed {run_id}" in toast for toast in toasts)
            assert app.log_lines[-1] == "── KILLED by operator ──"
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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(
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

    app = VelaApp(
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

    app = VelaApp(
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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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

    app = VelaApp(
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


def _quit_stop_target_client(*, stop_fails: bool = False):
    class _QuitStopTargetClient:
        def __init__(self) -> None:
            self.agent = RecordingConfigAgent()
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
                if stop_fails:
                    raise RuntimeError("target unreachable")
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("quit-stop tests should not subscribe")

    return _QuitStopTargetClient()


@pytest.mark.asyncio
async def test_quit_stop_pops_confirm_modal_immediately_and_notifies(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-234 bullet 1: confirming quit-stop must pop the ConfirmScreen right
    # away and show a "Stopping run …" notification, not leave the modal up.
    app = VelaApp(configs_dir=config_dir, target_client=_quit_stop_target_client())
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"
        app.target_connection_state = "connected"
        monkeypatch.setattr(
            app, "notify", lambda message, *args, **kwargs: notifications.append(str(message))
        )

        async def _noop_exit_after(*_args, **_kwargs) -> None:
            return

        monkeypatch.setattr(app, "_exit_after_target_run_exit", _noop_exit_after)

        app.push_screen(
            ConfirmScreen("Attached server is still running. Stop it before quit?")
        )
        await pilot.pause()
        assert app.screen.id == "confirm"

        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.id != "confirm"
        assert any("Stopping run" in message for message in notifications)


@pytest.mark.asyncio
async def test_target_stop_run_reports_success_and_failure(config_dir: Path) -> None:
    # bug-234 bullet 3: _target_stop_run must return True on a successful stop
    # and False (still surfacing the error text) when the stop RPC raises, so
    # the quit path can render a failure instead of waiting on current_run_id
    # forever.
    ok_app = VelaApp(configs_dir=config_dir, target_client=_quit_stop_target_client())
    async with ok_app.run_test() as pilot:
        await pilot.pause()
        result = await ok_app._target_stop_run(
            "run-1", interrupt_timeout=2, terminate_timeout=2
        )
        assert result is True

    fail_app = VelaApp(
        configs_dir=config_dir,
        target_client=_quit_stop_target_client(stop_fails=True),
    )
    async with fail_app.run_test() as pilot:
        await pilot.pause()
        result = await fail_app._target_stop_run(
            "run-9", interrupt_timeout=2, terminate_timeout=2
        )
        assert result is False
        assert "Unable to stop run-9" in fail_app.error_text


@pytest.mark.asyncio
async def test_quit_stop_wait_is_bounded_and_renders_unreachable_banner(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-234 bullet 2: the wait for current_run_id to clear is bounded by an
    # injectable timeout. On timeout render the unreachable banner and DO NOT
    # exit the app (no surprise quit minutes later).
    app = VelaApp(configs_dir=config_dir, target_client=_quit_stop_target_client())
    app._quit_stop_wait_timeout_seconds = 0.2
    exit_calls: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-7"  # never cleared -> the bounded wait times out
        app.target_connection_state = "connected"
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))

        app.confirm_stop_running()
        await _wait_for_condition(
            lambda: "Unable to stop run" in app.error_text
            and "target unreachable" in app.error_text,
            "quit-stop timeout banner was not rendered",
        )

        assert exit_calls == []
        assert app.is_running
        assert app.current_run_id == "run-7"


@pytest.mark.asyncio
async def test_quit_stop_failed_stop_rpc_does_not_exit_or_hang(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-234 bullets 2+3: if the stop RPC itself fails, the quit path renders a
    # failure immediately instead of waiting on current_run_id forever, and it
    # never exits the app.
    app = VelaApp(
        configs_dir=config_dir,
        target_client=_quit_stop_target_client(stop_fails=True),
    )
    app._quit_stop_wait_timeout_seconds = 30.0  # a hang here would be caught, not masked
    exit_calls: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-5"  # stays set; a hang would poll it forever
        app.target_connection_state = "connected"
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))

        app.confirm_stop_running()
        # The quit path must render its own failure banner ("… target
        # unreachable?") immediately rather than swallowing the error and
        # polling current_run_id forever.
        await _wait_for_condition(
            lambda: "target unreachable?" in app.error_text,
            "failed quit-stop did not render the unreachable banner",
        )

        # Give any (buggy) unbounded wait a chance to wrongly call exit.
        for _ in range(4):
            await pilot.pause()
            await asyncio.sleep(0.05)
        assert exit_calls == []
        assert app.is_running


@pytest.mark.asyncio
async def test_cancel_quit_confirm_cancels_quit_worker_so_no_zombie_exit(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-234 bullet 4: cancelling the confirm must cancel the "quit" worker
    # group so a lingering quit-stop worker cannot exit the app by surprise
    # once current_run_id eventually clears.
    app = VelaApp(configs_dir=config_dir, target_client=_quit_stop_target_client())
    app._quit_stop_wait_timeout_seconds = 5.0  # keep the worker in its wait loop
    exit_calls: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-3"
        app.target_connection_state = "connected"
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))

        # Start the quit-stop worker; it will sit in the bounded wait on run-3.
        app.confirm_stop_running()
        await _wait_for_condition(
            lambda: any(method == "stop" for method, _ in app._target_client.calls),
            "quit-stop worker did not request stop",
        )

        # Simulate the operator cancelling the confirm.
        app.push_screen(
            ConfirmScreen("Attached server is still running. Stop it before quit?")
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        # A cancelled quit group means clearing the run id must NOT exit the app.
        app.current_run_id = None
        for _ in range(6):
            await pilot.pause()
            await asyncio.sleep(0.05)
        assert exit_calls == []
        assert app.is_running


@pytest.mark.asyncio
async def test_quit_while_disconnected_with_live_run_offers_quit_without_stop(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-234 follow-up bullet 1: with a live attached run and an unreachable
    # target, stop/kill/detach/target-switch are all blocked too, so a plain
    # disconnect banner would leave NO in-app way to quit. Quit must instead
    # open a "quit without stopping" ConfirmScreen variant.
    app = VelaApp(configs_dir=config_dir, target_client=_quit_stop_target_client())
    exit_calls: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"
        app.target_connection_state = "disconnected"
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))

        app.action_quit()
        await pilot.pause()

        assert app.screen.id == "confirm"
        screen = app.screen
        assert isinstance(screen, ConfirmScreen)
        assert "target unreachable" in screen.title
        assert "Cannot stop run run-1 from here" in screen.message
        assert "leave it running on the target" in screen.message
        assert screen.confirm_label == "Quit without stopping"
        assert screen.confirm_action == "confirm_quit_without_stop"
        assert exit_calls == []
        assert all(method != "stop" for method, _ in app._target_client.calls)


@pytest.mark.asyncio
async def test_cancel_quit_without_stop_confirm_keeps_app_running_and_run_untouched(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-234 follow-up bullet 3: cancelling the disconnected quit variant keeps
    # the TUI running with the run untouched (no exit, no stop RPC).
    app = VelaApp(configs_dir=config_dir, target_client=_quit_stop_target_client())
    exit_calls: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"
        app.target_connection_state = "disconnected"
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))

        app.action_quit()
        await pilot.pause()
        assert app.screen.id == "confirm"

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen.id != "confirm"
        assert exit_calls == []
        assert app.is_running
        assert app.current_run_id == "run-1"
        assert all(method != "stop" for method, _ in app._target_client.calls)


@pytest.mark.asyncio
async def test_confirm_quit_without_stop_exits_without_stop_rpc_and_quiets_monitors(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-234 follow-up bullet 2: confirming the disconnected quit variant exits
    # WITHOUT calling the stop RPC, after cancelling the local monitor workers
    # the same way detach does (tail + health groups) so exit is quiet.
    app = VelaApp(configs_dir=config_dir, target_client=_quit_stop_target_client())
    exit_calls: list[bool] = []
    cancelled_groups: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"
        app.target_connection_state = "disconnected"
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: exit_calls.append(True))
        real_cancel_group = app.workers.cancel_group

        def spy_cancel_group(node, group):
            cancelled_groups.append(group)
            return real_cancel_group(node, group)

        monkeypatch.setattr(app.workers, "cancel_group", spy_cancel_group)

        app.action_quit()
        await pilot.pause()
        assert app.screen.id == "confirm"

        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.id != "confirm"
        assert exit_calls == [True]
        assert "tail" in cancelled_groups
        assert "health" in cancelled_groups
        assert all(method != "stop" for method, _ in app._target_client.calls)


@pytest.mark.asyncio
async def test_log_filter_and_search_are_functional(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app._write_log("slow load", "WARNING")
        app._write_log("ready", "INFO")

        app.apply_log_filter("WARN")
        await pilot.pause()

        assert app.visible_log_lines == ["slow load"]


@pytest.mark.asyncio
async def test_search_key_prompts_and_applies_submitted_text(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app._write_log("INFO ready")
        app._write_log("ERROR bad thing", "ERROR")
        await pilot.press("/", "b", "a", "d", "enter")
        await pilot.pause()

        assert app.search_text == "bad"
        assert app.search_matches == ["ERROR bad thing"]


@pytest.mark.asyncio
async def test_filter_key_prompts_and_applies_submitted_text(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app._write_log("INFO ready")
        app._write_log("ERROR bad thing", "ERROR")
        await pilot.press("f", "E", "R", "R", "O", "R", "enter")
        await pilot.pause()

        assert app.filter_text == "ERROR"
        assert app.visible_log_lines == ["ERROR bad thing"]


@pytest.mark.asyncio
async def test_log_search_highlights_matching_text_in_view(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
    app = VelaApp(configs_dir=config_dir)

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
async def test_progress_line_uses_figma_track_ticks_and_percent(
    config_dir: Path,
) -> None:
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        app.handle_log_record(
            LogRecord(
                "transient",
                "Loading safetensors checkpoint shards: 68% | 29/42 [01:07<00:24]",
                None,
            )
        )
        await pilot.pause()

        label = app.query_one("#progress-label", Static).content
        sublabel = app.query_one("#progress-text", Static).content
        track = app.query_one("#progress-track", Static).content
        percent = app.query_one("#progress-percent", Static).content

        assert isinstance(label, Text)
        assert isinstance(sublabel, Text)
        assert isinstance(track, Text)
        assert isinstance(percent, Text)
        assert label.plain == "Loading safetensors checkpoint shards"
        assert "29/42" in sublabel.plain
        assert track.plain.count("│") == 9
        assert "━" in track.plain
        assert "─" in track.plain
        assert percent.plain == "68%"
        assert _text_uses_style(track, tui_app_module.WARN)
        assert _text_uses_style(percent, tui_app_module.WARN)


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
    app = VelaApp(configs_dir=config_dir)

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
        # bug-237: the overlay renders the real Configs card ("Config"), not the
        # meta spec-note ("Sidebar overlay" / "Log remains primary…").
        assert "Config" in sidebar_overlay.content.plain
        assert "Sidebar overlay" not in sidebar_overlay.content.plain
        assert "Log remains primary" not in sidebar_overlay.content.plain
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


# bug-237: unchecked vs checked Checkbox states must be unambiguous. The dim
# slate box (#56707c = TEXT_FAINT) reads clearly as "off"; the green box
# (#67e8a5 = GREEN) reads clearly as "on". Assert the resolved component style,
# not pixels. Default Textual gives BOTH states the same near-invisible bg.
_CHECKBOX_OFF_BG = Color.parse("#56707c")
_CHECKBOX_ON_BG = Color.parse("#67e8a5")


@pytest.mark.asyncio
async def test_flag_manager_changed_only_checkbox_states_are_visible(
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
        "    print('  --tensor-parallel-size INTEGER')\n",
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
        """,
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("F")
        await pilot.pause()
        assert app.screen.id == "flag-manager"
        checkbox = app.screen.query_one("#flag-manager-changed-only", Checkbox)

        off = checkbox.get_component_styles("toggle--button")
        assert off.background == _CHECKBOX_OFF_BG
        assert off.background != _CHECKBOX_ON_BG

        checkbox.value = True
        await pilot.pause()
        on = checkbox.get_component_styles("toggle--button")
        assert on.background == _CHECKBOX_ON_BG


@pytest.mark.asyncio
async def test_wizard_download_now_checkbox_states_are_visible(config_dir: Path) -> None:
    class ComposerClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_presets":
                return {
                    "presets": [
                        {
                            "name": "balanced",
                            "description": "Balanced",
                            "engine": {},
                            "extra_args": [],
                            "applies_to": ["all"],
                        }
                    ]
                }
            if method == "discover_runs":
                return {"runs": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            default = _optional_wizard_section_result(method)
            if default is not None:
                return default
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            raise AssertionError("checkbox-style test should not subscribe")

    app = VelaApp(configs_dir=config_dir, target_client=ComposerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert app.screen.id == "new-deployment"
        checkbox = app.screen.query_one("#new-deployment-download-now", Checkbox)

        off = checkbox.get_component_styles("toggle--button")
        assert off.background == _CHECKBOX_OFF_BG
        assert off.background != _CHECKBOX_ON_BG

        checkbox.value = True
        await pilot.pause()
        on = checkbox.get_component_styles("toggle--button")
        assert on.background == _CHECKBOX_ON_BG


# --- Task 4.5: adaptive truthful top chrome (bug-237) ---

_HEADER_WIDTHS = (80, 100, 120, 140)


def _write_header_config(config_dir: Path) -> None:
    write_yaml(
        config_dir / "demo.yaml",
        """
        name: demo-model
        model: org/demo-model
        server:
          host: 127.0.0.1
          port: 8765
        """,
    )


def test_truncate_cells_is_cell_aware_and_never_splits_a_wide_glyph() -> None:
    from vela.tui.cells import truncate_cells

    assert truncate_cells("hello", 10) == "hello"
    assert truncate_cells("hello world", 8) == "hello w…"
    assert truncate_cells("anything", 0) == ""
    # A leading double-width emoji must not be split across the boundary.
    pinned = truncate_cells("📌abcdef", 4)
    assert pinned.endswith("…")
    assert cell_len(pinned) <= 4
    # CJK glyphs are two cells each; the result must stay within budget.
    cjk = truncate_cells("中文字符名称", 5)
    assert cell_len(cjk) <= 5
    assert cjk.endswith("…")


@pytest.mark.asyncio
async def test_top_chrome_badge_stays_in_flow_and_fully_visible(config_dir: Path) -> None:
    _write_header_config(config_dir)
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        for width in _HEADER_WIDTHS:
            await pilot.resize_terminal(width, 40)
            await pilot.pause()
            top = app.query_one("#top-chrome").region
            badge = app.query_one("#status-badge").region
            # Fully inside the header horizontally: both side borders visible.
            assert badge.x >= top.x, f"badge left-clipped at {width}"
            assert badge.x + badge.width <= top.x + top.width, (
                f"badge right-overflows header at {width}: "
                f"badge_right={badge.x + badge.width} header_right={top.x + top.width}"
            )
            # Full bordered box (3 rows) and within the header vertically.
            assert badge.height >= 3, f"badge lost its border height at {width}"
            assert badge.y >= top.y
            assert badge.y + badge.height <= top.y + top.height


@pytest.mark.asyncio
async def test_top_chrome_priority_collapse_right_to_left(config_dir: Path) -> None:
    _write_header_config(config_dir)
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        # Invariant at every width: header height fixed, no segment wraps, and no
        # segment content is clipped mid-glyph (content always fits its region).
        for width in _HEADER_WIDTHS:
            await pilot.resize_terminal(width, 40)
            await pilot.pause()
            top = app.query_one("#top-chrome").region
            assert top.height == 3, f"header height drifted at {width}"
            for selector in (
                "#app-title",
                "#target-segment",
                "#active-model",
                "#server-url",
                "#chrome-clock",
            ):
                widget = app.query_one(selector, Static)
                if not widget.display:
                    continue
                content = str(widget.content)
                assert "\n" not in content, f"{selector} wrapped at {width}"
                assert cell_len(content) <= widget.region.width, (
                    f"{selector} clipped at {width}: "
                    f"{content!r} ({cell_len(content)} cells) > {widget.region.width}"
                )

        # 80: badge + target visible; URL and clock collapse away (right-to-left).
        await pilot.resize_terminal(80, 40)
        await pilot.pause()
        assert str(app.query_one("#target-segment", Static).content) != ""
        assert app.query_one("#server-url", Static).display is False
        assert app.query_one("#chrome-clock", Static).display is False
        assert "demo-model" in str(app.query_one("#active-model", Static).content)

        # 100: model readable (fully rendered, not ellipsized).
        await pilot.resize_terminal(100, 40)
        await pilot.pause()
        model_100 = str(app.query_one("#active-model", Static).content)
        assert "model: demo-model" in model_100
        assert not model_100.rstrip().endswith("…")

        # 120: URL present again.
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert app.query_one("#server-url", Static).display is True
        assert "8765" in str(app.query_one("#server-url", Static).content)

        # 140: everything, clock included.
        await pilot.resize_terminal(140, 40)
        await pilot.pause()
        assert app.query_one("#server-url", Static).display is True
        assert app.query_one("#chrome-clock", Static).display is True
        clock = str(app.query_one("#chrome-clock", Static).content)
        assert re.match(r"\d\d:\d\d:\d\d", clock), f"clock not shown at 140: {clock!r}"


# --- Task 4.6: sidebar vertical fit (bug-237) ---


def _write_sidebar_config(config_dir: Path) -> None:
    write_yaml(
        config_dir / "sidebar-demo.yaml",
        """
        name: sidebar-demo
        model: org/sidebar-demo
        server:
          host: 127.0.0.1
          port: 8765
        engine:
          tensor_parallel_size: 2
          kv_cache_dtype: fp8
        """,
    )


_SIDEBAR_CARDS = ("#config-panel", "#phase-panel", "#gpu-panel", "#status-strip")


@pytest.mark.asyncio
async def test_sidebar_cards_fit_short_terminal(config_dir: Path) -> None:
    # bug-237: at 100x30 the fixed-height cards overflowed — the GPU panel was
    # clipped 5 rows and the status strip fell fully off-screen. Every card must
    # now render at least its title + first content line inside the sidebar.
    _write_sidebar_config(config_dir)
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen_h = app.size.height
        sidebar = app.query_one("#sidebar").region
        for selector in _SIDEBAR_CARDS:
            card = app.query_one(selector)
            assert card.display is True, f"{selector} hidden at 100x30"
            region = card.region
            assert region.height > 0, f"{selector} collapsed at 100x30"
            # The card's top row (title + first content line) is inside the
            # sidebar's visible region, so nothing renders off-screen.
            assert sidebar.y <= region.y < sidebar.y + sidebar.height, (
                f"{selector} top not in sidebar viewport: y={region.y} "
                f"sidebar=[{sidebar.y},{sidebar.y + sidebar.height})"
            )
            assert region.y < screen_h, f"{selector} top off-screen at 100x30"

        gpu = app.query_one("#gpu-panel").region
        assert gpu.y + gpu.height <= screen_h, (
            f"GPU panel clipped at 100x30: bottom={gpu.y + gpu.height} screen={screen_h}"
        )
        status = app.query_one("#status-strip").region
        assert 0 <= status.y < screen_h, (
            f"status strip off-screen at 100x30: y={status.y} screen={screen_h}"
        )


@pytest.mark.asyncio
async def test_sidebar_cards_hug_content_on_tall_terminal(config_dir: Path) -> None:
    # bug-237: with fixed heights the Phases card reserved 11 rows for its single
    # IDLE line and the status strip still fell past the fold at 38 rows. Auto
    # heights must hug the real content and keep every card fully on-screen.
    _write_sidebar_config(config_dir)
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(142, 38)) as pilot:
        await pilot.pause()
        screen_h = app.size.height
        phase = app.query_one("#phase-panel").region
        # One IDLE phase line ("Phases" + "○ IDLE --") in a bordered card is
        # ~3-4 rows, never the old fixed 11.
        assert phase.height <= 6, f"Phases card did not hug content: h={phase.height}"

        status = app.query_one("#status-strip").region
        assert status.height > 0
        assert status.y + status.height <= screen_h, (
            f"status strip off-screen at 142x38: bottom={status.y + status.height} "
            f"screen={screen_h}"
        )


@pytest.mark.asyncio
async def test_sidebar_hides_gpu_panel_when_terminal_too_short(config_dir: Path) -> None:
    # bug-237: _apply_responsive_layout must key on HEIGHT too — when the vertical
    # budget can no longer hold every card, drop the GPU monitor first (mirroring
    # the compact-width behaviour) while config/phase stay visible.
    _write_sidebar_config(config_dir)
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # 30 rows is enough for all four cards.
        assert app.query_one("#gpu-panel").display is True

        await pilot.resize_terminal(100, 22)
        await pilot.pause()
        # Too short now: GPU sheds first, config + phase remain.
        assert app.query_one("#gpu-panel").display is False, "GPU not hidden at 100x22"
        assert app.query_one("#config-panel").display is True
        assert app.query_one("#phase-panel").display is True


# --- Task 4.7: context-sensitive footer that always fits (bug-237) ---


def _footer_rows(app: VelaApp) -> list[str]:
    return str(app.query_one("#footer-bindings", Static).content).split("\n")


def _footer_text(app: VelaApp) -> str:
    return str(app.query_one("#footer-bindings", Static).content)


@pytest.mark.asyncio
async def test_footer_hides_inert_actions_at_idle(config_dir: Path) -> None:
    # bug-237: at IDLE with no run the control keys (Stop/Kill/Restart) and the
    # run-log keys (Search/Filter/Pause/Wrap/Top-Bottom) are inert, so the footer
    # must not advertise them. `? Help` and `q Quit` always render.
    _write_header_config(config_dir)
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert app.phase is Phase.IDLE
        content = _footer_text(app)
        for hidden in (
            "s Stop",
            "K Kill",
            "r Restart",
            "/ Search",
            "f Filter",
            "p Pause",
            "w Wrap",
            "Top/Bottom",
        ):
            assert hidden not in content, f"{hidden!r} advertised at idle"
        assert "? Help" in content
        assert "q Quit" in content
        # It also fits: no row exceeds the footer width, cell-aware.
        footer_w = app.query_one("#footer-bindings", Static).region.width
        for row in _footer_rows(app):
            assert cell_len(row) <= footer_w, f"footer row overflows 80: {row!r}"


@pytest.mark.asyncio
async def test_footer_reconnect_only_when_not_connected(config_dir: Path) -> None:
    # bug-237: `R Reconnect` is inert at a healthy IDLE — it only applies once the
    # target link is not connected.
    _write_header_config(config_dir)
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.target_connection_state == "connected"
        assert "R Reconnect" not in _footer_text(app), "Reconnect shown at healthy IDLE"

        app.target_connection_state = "disconnected"
        app._refresh_dashboard_shell()
        await pilot.pause()
        assert "R Reconnect" in _footer_text(app), "Reconnect missing when disconnected"


@pytest.mark.asyncio
async def test_footer_shows_controls_when_running_and_keeps_help_quit(
    config_dir: Path,
) -> None:
    # bug-237: with a live attached run the control + log keys return; if the
    # width cannot fit them all, hints drop from a priority tail but `? Help` and
    # `q Quit` are pinned at every width.
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
    app = VelaApp(configs_dir=config_dir)

    try:
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("l")
            await _wait_for_log(app, "Uvicorn running")
            await _wait_for_phase(app, Phase.READY)
            for width in (80, 100, 142):
                await pilot.resize_terminal(width, 40)
                await pilot.pause()
                content = _footer_text(app)
                assert "s Stop" in content, f"control key missing at {width}"
                assert "/ Search" in content, f"log key missing at {width}"
                assert "? Help" in content, f"Help dropped at {width}"
                assert "q Quit" in content, f"Quit dropped at {width}"
                footer_w = app.query_one("#footer-bindings", Static).region.width
                for row in _footer_rows(app):
                    assert cell_len(row) <= footer_w, (
                        f"footer row overflows {width}: {row!r} "
                        f"({cell_len(row)} > {footer_w})"
                    )
            await pilot.press("s")
            await _wait_for_stopped(app)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_server_url_is_dim_until_ready_then_live(config_dir: Path) -> None:
    _write_header_config(config_dir)
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        url = app.query_one("#server-url", Static)
        # IDLE: the configured URL shows dim (muted), never the live colour.
        assert app.phase is Phase.IDLE
        content = url.content
        assert isinstance(content, Text)
        assert "127.0.0.1:8765" in content.plain
        assert _text_uses_style(content, "#8ba4ae"), f"IDLE url not dim: {content!r}"
        assert not _text_uses_style(content, "#67e8a5"), "IDLE url must not read as live"

        # READY: the live server URL turns the ready colour.
        app.ready_url = "http://127.0.0.1:8765"
        app._set_phase(Phase.READY)
        await pilot.pause()
        content = app.query_one("#server-url", Static).content
        assert isinstance(content, Text)
        assert _text_uses_style(content, "#67e8a5"), f"READY url not live: {content!r}"

        # DEGRADED: still serving (flapping health) — amber live, never dim.
        app._set_phase(Phase.DEGRADED)
        await pilot.pause()
        content = app.query_one("#server-url", Static).content
        assert isinstance(content, Text)
        assert _text_uses_style(content, "#f6c85f"), f"DEGRADED url not amber: {content!r}"
        assert not _text_uses_style(content, "#8ba4ae"), "DEGRADED url must not read dim"

        # STOPPED: back to dim — nothing is live any more.
        app._set_phase(Phase.STOPPED)
        await pilot.pause()
        content = app.query_one("#server-url", Static).content
        assert isinstance(content, Text)
        assert _text_uses_style(content, "#8ba4ae"), f"STOPPED url not dim: {content!r}"
        assert not _text_uses_style(content, "#67e8a5"), "STOPPED url must not read as live"


@pytest.mark.asyncio
async def test_log_buffers_are_bounded_for_bursty_output(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir, max_log_lines=3)

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
    app = VelaApp(configs_dir=config_dir, debug_log_path=debug_log_path)

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
    app = VelaApp(configs_dir=config_dir)

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


async def _wait_for_log(app: VelaApp, text: str) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        if any(text in line for line in app.log_lines):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"log line {text!r} was not emitted")


async def _wait_for_command(app: VelaApp, title: str):
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        for command in app.get_system_commands(app.screen):
            if command.title == title:
                return command
        await asyncio.sleep(0.05)
    raise AssertionError(f"command {title!r} was not available")


def _non_discovery_target_calls(app: VelaApp):
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


async def _reattach_discovered_target_run(app: VelaApp, run_id: str) -> None:
    await app._refresh_detached_runs()
    assert any(run["run_id"] == run_id for run in app.detached_run_summaries)
    await app._reattach_target_detached_run(run_id)


async def _wait_for_gpu_text(app: VelaApp, text: str) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        if text in app.gpu_panel_text:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"GPU panel text {text!r} was not shown")


async def _wait_for_gpu_calls(calls: list[int], count: int) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        if len(calls) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"GPU sampler was called {len(calls)} times, expected {count}")


async def _wait_for_target_connection_state(
    app: VelaApp, state: str, *, timeout: float = 5.0
) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(timeout)
    while asyncio.get_running_loop().time() < deadline:
        if app.target_connection_state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"target connection state was {app.target_connection_state!r}, expected {state!r}"
    )


async def _wait_for_stopped(app: VelaApp) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        if app.current_run_id is None and app.phase is Phase.STOPPED:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("fake child did not stop")


async def _wait_for_condition(condition, message: str) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


async def _wait_for_textual_condition(
    pilot,
    condition,
    message: str,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(timeout)
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            if condition():
                return
        except Exception as exc:  # pragma: no cover - surfaced in assertion context
            last_error = exc
        await pilot.pause()
    if last_error is not None:
        raise AssertionError(f"{message}; last condition error: {last_error!r}") from last_error
    raise AssertionError(message)


async def _wait_for_log_count(app: VelaApp, text: str, count: int) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        if sum(text in line for line in app.log_lines) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"log line {text!r} did not reach count {count}")


async def _wait_for_phase(app: VelaApp, phase: Phase, *, pilot=None) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        if app.phase is phase:
            return
        if pilot is not None:
            await pilot.pause()
        else:
            await asyncio.sleep(0.05)
    log_tail = "\n".join(app.log_lines[-5:])
    raise AssertionError(
        f"phase {phase.value} was not reached; "
        f"current={app.phase.value} error={app.error_text!r} health={app.health_detail!r} "
        f"log_tail={log_tail!r}"
    )


async def _wait_for_log_text(path: Path, text: str) -> None:
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
    while asyncio.get_running_loop().time() < deadline:
        if path.exists() and text in path.read_text(encoding="utf-8"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{text!r} was not written to {path}")


def _static_text(app: VelaApp, selector: str) -> str:
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
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
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
    deadline = asyncio.get_running_loop().time() + scaled_timeout(5)
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


@pytest.mark.asyncio
async def test_sidebar_config_summary_never_wraps_names_mid_word(config_dir: Path) -> None:
    write_yaml(
        config_dir / "long-name.yaml",
        """
        name: qwen36-27b-bf16-blackwell-canary-extra-long
        model: org/model
        """,
    )
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        summary = app._render_config_summary()
        # Screenshot-#1 fix: long config names truncate with an ellipsis
        # instead of wrapping mid-word in the sidebar.
        assert summary.no_wrap is True
        assert summary.overflow == "ellipsis"


@pytest.mark.asyncio
async def test_post_ready_health_failure_degrades_then_recovers(config_dir: Path) -> None:
    # FR-18: health polling must continue after READY — a live server that goes
    # unhealthy flips to DEGRADED, and a later 200 recovers to READY.
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "degrade.yaml",
        f"""
        name: degrade
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
    app = VelaApp(configs_dir=config_dir)

    try:
        async with app.run_test() as pilot:
            await pilot.press("l")
            await _wait_for_phase(app, Phase.READY)
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.get(f"http://127.0.0.1:{port}/admin/health-off")
            await _wait_for_phase(app, Phase.DEGRADED)
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.get(f"http://127.0.0.1:{port}/admin/health-on")
            await _wait_for_phase(app, Phase.READY)
            await pilot.press("s")
            await _wait_for_port_down(port)
    finally:
        await _cleanup_port(port)


class _JourneyComposerClient:
    """Minimal composer-RPC fake for the journey-friction wizard tests."""

    connected = False

    def __init__(self, *, fail_validation: bool = False) -> None:
        self.fail_validation = fail_validation
        self.calls: list[tuple[str, dict | None]] = []
        self.saved_config: dict | None = None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def call(self, method: str, params):
        self.calls.append((method, params))
        if method == "list_configs":
            return {"valid": [], "invalid": []}
        if method == "list_presets":
            return {
                "presets": [
                    {
                        "name": "balanced",
                        "description": "Balanced",
                        "engine": {},
                        "extra_args": [],
                        "applies_to": ["all"],
                    }
                ]
            }
        if method == "compose_config":
            return {
                "config": {
                    "name": params["name"],
                    "target": "local",
                    "model": params.get("model", "Qwen/Qwen3-32B"),
                    "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
                },
                "warnings": [],
                "derived": [],
            }
        if method == "validate_config":
            if self.fail_validation:
                return {
                    "ok": False,
                    "errors": ["server.port: port 18001 is already taken"],
                    "warnings": [],
                }
            return {"ok": True, "errors": [], "warnings": []}
        if method == "preview":
            return {"preview": "cwd=/agent\nvllm serve Qwen/Qwen3-32B", "warnings": []}
        if method == "preflight":
            return {"ok": True, "failures": []}
        if method == "save_config":
            self.saved_config = dict(params["config"])
            return {
                "path": "/agent/configs/qwen3.yaml",
                "name": params["config"]["name"],
                "config": self.saved_config,
            }
        if method == "discover_runs":
            return {"runs": []}
        if method in {"gpu", "sample_gpus"}:
            return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
        raise AssertionError(f"unexpected target client call: {method}")

    def subscribe(self, *_args, **_kwargs):
        raise AssertionError("journey wizard tests should not subscribe")


@pytest.mark.asyncio
async def test_new_deployment_draft_survives_validation_failure(config_dir: Path) -> None:
    # J1: a server-side validation failure must NOT discard the wizard draft —
    # the wizard reopens with every typed value intact and the error inside it.
    app = VelaApp(
        configs_dir=config_dir,
        target_client=_JourneyComposerClient(fail_validation=True),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert app.screen.id == "new-deployment"
        app.screen.query_one("#new-deployment-name", Input).value = "qwen3"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        app.screen.query_one("#new-deployment-port", Input).value = "18001"
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-name", Input).value == "qwen3",
            "wizard did not reopen with the draft after validation failure",
        )
        assert app.screen.query_one("#new-deployment-model", Input).value == "Qwen/Qwen3-32B"
        assert app.screen.query_one("#new-deployment-port", Input).value == "18001"
        error_text = str(
            app.screen.query_one("#new-deployment-error", Static).content
        )
        assert "already taken" in error_text


@pytest.mark.asyncio
async def test_new_deployment_review_back_returns_to_wizard_with_draft(
    config_dir: Path,
) -> None:
    # J2: Back from the Review screen reopens the wizard with the draft intact.
    app = VelaApp(configs_dir=config_dir, target_client=_JourneyComposerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#new-deployment-name", Input).value = "qwen3"
        app.screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "review screen did not open",
        )
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-name", Input).value == "qwen3",
            "back did not reopen the wizard with the draft",
        )
        assert app.screen.query_one("#new-deployment-model", Input).value == "Qwen/Qwen3-32B"


@pytest.mark.asyncio
async def test_new_deployment_enter_advances_steps_without_submitting(
    config_dir: Path,
) -> None:
    # J3: Enter in a wizard input advances one step; the whole wizard only
    # submits from the Review step (Ctrl+S still works from anywhere).
    app = VelaApp(configs_dir=config_dir, target_client=_JourneyComposerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert screen.id == "new-deployment"
        name_input = screen.query_one("#new-deployment-name", Input)
        name_input.value = "qwen3"
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        name_input.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # Still the wizard, advanced one step — NOT submitted.
        assert app.screen is screen
        assert screen.step_index == 1
        # March to the Review step; Enter there submits.
        for _ in range(3):
            await pilot.press("ctrl+n")
        assert screen.step_index == 4
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "enter on the review step did not submit",
        )


@pytest.mark.asyncio
async def test_golden_path_journey_survives_failure_and_back(config_dir: Path) -> None:
    # J4: the Phase-A regression net. Cold start -> n -> Enter-walk the steps ->
    # server-side validation failure (draft survives, error in-wizard) ->
    # resubmit -> Review -> Back (draft survives) -> Review -> Save.
    client = _JourneyComposerClient(fail_validation=True)
    app = VelaApp(configs_dir=config_dir, target_client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        screen = app.screen
        assert screen.id == "new-deployment"
        name_input = screen.query_one("#new-deployment-name", Input)
        name_input.value = "qwen3"
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        name_input.focus()
        await pilot.pause()
        # Enter walks Target -> Runtime -> Model -> Customize -> Review.
        for expected_step in (1, 2, 3, 4):
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is screen, "enter must not leave the wizard mid-walk"
            assert screen.step_index == expected_step
        # Enter on the Review step submits; validation fails server-side.
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen is not screen
            and app.screen.query_one("#new-deployment-name", Input).value == "qwen3",
            "draft did not survive the validation failure",
        )
        assert "already taken" in str(
            app.screen.query_one("#new-deployment-error", Static).content
        )
        # Fix the server-side condition and resubmit.
        client.fail_validation = False
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "review did not open after resubmit",
        )
        # Back keeps the draft.
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and app.screen.query_one("#new-deployment-name", Input).value == "qwen3",
            "back did not restore the wizard draft",
        )
        # Forward again and save.
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment-review",
            "review did not reopen",
        )
        await pilot.press("ctrl+s")
        await _wait_for_condition(
            lambda: app.current_config is not None and app.current_config.name == "qwen3",
            "save did not select the new config",
        )
        assert client.saved_config is not None


@pytest.mark.asyncio
async def test_footer_advertises_new_deployment_and_configs(config_dir: Path) -> None:
    # J11: the flagship flow's key must be visible in the persistent footer —
    # and early enough to survive right-side truncation at narrow widths. The
    # footer is now context/width-fitted (bug-237) and returns a Rich Text, so
    # assert against its packed plain text at a wide width where all nav hints show.
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        footer = app._render_footer_bindings(120).plain
        assert "n New" in footer
        assert "c Configs" in footer
        assert footer.index("n New") < footer.index("t Targets")


@pytest.mark.asyncio
async def test_empty_dashboard_points_to_first_actions(config_dir: Path) -> None:
    # J12+J13: a fresh install must not be a dead end — the empty Configs
    # panel names the first action and the log pane carries a quick start.
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "press n" in app.config_summary
        assert any("Quick start" in line for line in app.log_lines)
        assert any("n  create a deployment" in line for line in app.log_lines)


@pytest.mark.asyncio
async def test_quick_start_absent_once_configs_exist(config_dir: Path) -> None:
    write_yaml(
        config_dir / "existing.yaml",
        """
        name: existing
        model: org/model
        """,
    )
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not any("Quick start" in line for line in app.log_lines)
        assert "press n" not in app.config_summary


@pytest.mark.asyncio
async def test_config_picker_empty_state_names_first_action(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.id == "config-picker"
        assert "press n" in app.screen.summary


@pytest.mark.asyncio
async def test_help_screen_explains_glyphs_and_journey(config_dir: Path) -> None:
    # J14: the marker glyphs and the journey spine are defined in Help.
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        help_text = str(app.screen.query_one("#help-text", Static).content)
        assert "📌" in help_text and "pinned" in help_text
        assert "🔒" in help_text
        assert "target × build × model@revision × config" in help_text


class _BridgeJobEvents:
    def __init__(self, done_payload: dict[str, object]) -> None:
        self.closed = False
        self._events: list[dict[str, object]] = []
        self._done_payload = done_payload

    def arm(self, job_id: str) -> None:
        self._events = [
            {
                "event": "job_progress",
                "job_id": job_id,
                "kind": "committed",
                "text": "working",
                "level": "INFO",
            },
            {"event": "job_done", "job_id": job_id, **self._done_payload},
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_create_build_success_bridges_back_to_build_manager(
    config_dir: Path,
) -> None:
    # J5+J10: announce the job start, then land the user back in the Build
    # Manager focused on the new build with the next step named.
    class BridgeBuildClient:
        connected = False

        def __init__(self) -> None:
            self.events = _BridgeJobEvents(
                {"ok": True, "detail": "build ready", "label": "nvfp4", "build_id": "01NEW"}
            )
            self.created = False
            self.list_builds_calls = 0

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_builds":
                self.list_builds_calls += 1
                builds = [
                    {"build_id": "01STABLE", "label": "stable-cu124", "status": "ready",
                     "default": True}
                ]
                if self.created:
                    builds.append(
                        {"build_id": "01NEW", "label": "nvfp4", "status": "ready"}
                    )
                return {"builds": builds, "skipped": []}
            if method == "check_build_prerequisites":
                return {"ok": True, "method": params["method"], "uv_available": True}
            if method == "create_build":
                self.created = True
                self.events.arm(str(params["job_id"]))
                return {"job_id": params["job_id"], "kind": "create_build", "status": "running"}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            return self.events

    client = BridgeBuildClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager", "build manager did not open"
        )
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "create-build", "create build did not open"
        )
        app.screen.query_one("#create-build-method", Select).value = "nightly"
        app.screen.query_one("#create-build-label", Input).value = "nvfp4"
        await pilot.press("enter")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager" and client.list_builds_calls >= 2,
            "build manager did not reopen after the job",
        )
        assert any("Build started" in note for note in notifications)
        ready_notes = [note for note in notifications if "Build ready: nvfp4" in note]
        assert ready_notes and "default" in ready_notes[0]
        # Focused on the new build.
        assert app.screen.selected_index == 1


@pytest.mark.asyncio
async def test_smoke_completion_bridges_to_launch(config_dir: Path) -> None:
    # J6: smoke pass ends with "press l to launch", not a silent STOPPED.
    class SmokeBridgeClient:
        connected = False

        def __init__(self) -> None:
            self.saved: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                if self.saved is None:
                    return {"valid": [], "invalid": []}
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/qwen3.yaml",
                            "name": "qwen3",
                            "model": "org/model",
                            "target": "local",
                            "warnings": [],
                            "config": self.saved,
                        }
                    ],
                    "invalid": [],
                }
            if method == "preflight":
                return {"ok": True, "failures": []}
            if method == "save_config":
                self.saved = dict(params["config"])
                return {"path": "/agent/configs/qwen3.yaml", "name": "qwen3", "config": self.saved}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/model", "warnings": []}
            if method == "prepare_launch":
                return {
                    "config": self.saved,
                    "build": {
                        "argv": ["/bin/echo", "ready"],
                        "env": {},
                        "cwd": "/agent",
                        "warnings": [],
                        "metadata": {},
                        "preview": "",
                    },
                    "preflight": None,
                }
            if method == "launch":
                return {"run_id": "smoke-run-1", "launch_mode": "attached", "status": "started"}
            if method == "probe_until_ready":
                return {
                    "run_id": "smoke-run-1",
                    "ready": True,
                    "detail": "ready",
                    "models": ["org/model"],
                    "reachable_url": "http://127.0.0.1:8000",
                    "phase": "READY",
                }
            if method == "stop":
                return {"run_id": "smoke-run-1", "signaled": True}
            if method == "wait":
                return {"run_id": "smoke-run-1", "returncode": 0, "intentional": True}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    app = VelaApp(configs_dir=config_dir, target_client=SmokeBridgeClient())
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        config = {
            "name": "qwen3",
            "target": "local",
            "model": "org/model",
            "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
        }
        await app._save_reviewed_new_deployment(config, smoke=True)
        await pilot.pause()
        assert any("Saved deployment: qwen3" in note for note in notifications)
        bridge = [note for note in notifications if "press l to launch" in note]
        assert bridge and "qwen3" in bridge[0]


@pytest.mark.asyncio
async def test_smoke_failure_bridges_to_flags_and_retry(config_dir: Path) -> None:
    class SmokeFailClient:
        connected = False

        def __init__(self) -> None:
            self.saved: dict | None = None

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                if self.saved is None:
                    return {"valid": [], "invalid": []}
                return {
                    "valid": [
                        {
                            "path": "/agent/configs/qwen3.yaml",
                            "name": "qwen3",
                            "model": "org/model",
                            "target": "local",
                            "warnings": [],
                            "config": self.saved,
                        }
                    ],
                    "invalid": [],
                }
            if method == "preflight":
                return {"ok": True, "failures": []}
            if method == "save_config":
                self.saved = dict(params["config"])
                return {"path": "/agent/configs/qwen3.yaml", "name": "qwen3", "config": self.saved}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/model", "warnings": []}
            if method == "prepare_launch":
                return {
                    "config": self.saved,
                    "build": {
                        "argv": ["/bin/echo", "ready"],
                        "env": {},
                        "cwd": "/agent",
                        "warnings": [],
                        "metadata": {},
                        "preview": "",
                    },
                    "preflight": None,
                }
            if method == "launch":
                return {"run_id": "smoke-run-2", "launch_mode": "attached", "status": "started"}
            if method == "probe_until_ready":
                return {
                    "run_id": "smoke-run-2",
                    "ready": False,
                    "detail": "readiness timeout after 1s; still loading",
                    "models": [],
                    "phase": "STARTING",
                }
            if method == "stop":
                return {"run_id": "smoke-run-2", "signaled": True}
            if method == "wait":
                return {"run_id": "smoke-run-2", "returncode": 0, "intentional": True}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    app = VelaApp(configs_dir=config_dir, target_client=SmokeFailClient())
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        config = {
            "name": "qwen3",
            "target": "local",
            "model": "org/model",
            "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
        }
        await app._save_reviewed_new_deployment(config, smoke=True)
        await pilot.pause()
        bridge = [note for note in notifications if "F adjust flags" in note]
        assert bridge and "saved" in bridge[0]


@pytest.mark.asyncio
async def test_download_success_bridges_back_to_model_manager(config_dir: Path) -> None:
    # J9: a finished download lands a toast and the refreshed Model Manager.
    class DownloadBridgeClient:
        connected = False

        def __init__(self) -> None:
            self.events = _BridgeJobEvents({"ok": True, "detail": "model cached"})
            self.downloaded = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_models":
                cache_state = "cached" if self.downloaded else "remote_only"
                return {
                    "models": [
                        {
                            "entry_id": "01MODEL",
                            "display_name": "llama-pin",
                            "source": "hf_repo",
                            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                            "model_ref": "meta-llama/Llama-3.1-8B-Instruct",
                            "revision": "main",
                            "cache_state": cache_state,
                            "gated": False,
                            "size_bytes": 0,
                            "files": {},
                        }
                    ],
                    "default_cache": "hf",
                    "app_download_dir": None,
                    "skipped": [],
                }
            if method == "download_model":
                self.downloaded = True
                self.events.arm(str(params["job_id"]))
                return {"job_id": params["job_id"], "kind": "download_model", "status": "running"}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            return self.events

    client = DownloadBridgeClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        app._handle_download_model_submission(
            {"model_ref": "meta-llama/Llama-3.1-8B-Instruct"}
        )
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not reopen after the download",
        )
        bridge = [note for note in notifications if "Downloaded" in note]
        assert bridge
        assert "meta-llama/Llama-3.1-8B-Instruct" in bridge[0]


@pytest.mark.asyncio
async def test_adopt_build_success_bridges_back_to_build_manager(
    config_dir: Path,
) -> None:
    # J5 (adopt half): adopting lands the user back in the Build Manager
    # focused on the adopted build, with the next step named.
    class AdoptBridgeClient:
        connected = False

        def __init__(self) -> None:
            self.adopted = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "adopt_build":
                self.adopted = True
                return {
                    "build_id": "01ADOPT",
                    "label": "vllm-nightly",
                    "status": "adopted",
                    "manifest": {},
                }
            if method == "list_builds":
                builds = [
                    {"build_id": "01STABLE", "label": "stable-cu124", "status": "ready",
                     "default": True}
                ]
                if self.adopted:
                    builds.append(
                        {"build_id": "01ADOPT", "label": "vllm-nightly", "status": "adopted"}
                    )
                return {"builds": builds, "skipped": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    app = VelaApp(configs_dir=config_dir, target_client=AdoptBridgeClient())
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        app._handle_adopt_build_submission(
            {"venv_path": "/home/user/venvs/vllm-nightly", "label": "vllm-nightly"}
        )
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager",
            "build manager did not reopen after adopt",
        )
        bridge = [note for note in notifications if "Adopted build: vllm-nightly" in note]
        assert bridge and "default" in bridge[0]
        assert app.screen.selected_index == 1


def test_hf_auth_guidance_names_token_location() -> None:
    # J18: the HF_AUTH error guidance points at where the token lives.
    from vela.tui.app import ERROR_GUIDANCE

    guidance = ERROR_GUIDANCE[ErrorKind.HF_AUTH]
    assert "HF_TOKEN" in guidance
    assert "agent" in guidance and "env" in guidance


@pytest.mark.asyncio
async def test_pin_success_bridges_back_to_model_manager(config_dir: Path) -> None:
    # J16: pinning lands the user in the Model Manager focused on the new
    # entry with the download hint.
    class PinBridgeClient:
        connected = False

        def __init__(self) -> None:
            self.pinned = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "pin_model":
                self.pinned = True
                return {
                    "entry": {"entry_id": "qwen-pin", "display_name": "qwen-pin"},
                    "warnings": [],
                }
            if method == "list_models":
                models = [
                    {"entry_id": "old-pin", "display_name": "old-pin",
                     "cache_state": "cached"}
                ]
                if self.pinned:
                    models.append(
                        {"entry_id": "qwen-pin", "display_name": "qwen-pin",
                         "cache_state": "remote_only"}
                    )
                return {"models": models, "default_cache": "hf", "skipped": []}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    app = VelaApp(configs_dir=config_dir, target_client=PinBridgeClient())
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        app._handle_pin_model_submission({"repo_id": "Qwen/Qwen3-32B"})
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager",
            "model manager did not reopen after pin",
        )
        bridge = [note for note in notifications if "Pinned model: qwen-pin" in note]
        assert bridge and "downloads it" in bridge[0]
        assert app.screen.selected_index == 1


@pytest.mark.asyncio
async def test_model_remove_confirm_states_reclaim_and_irreversibility(
    config_dir: Path,
) -> None:
    # J17: the remove confirm names the reclaimed cache and irreversibility.
    class RemoveConfirmClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_models":
                return {
                    "models": [
                        {
                            "entry_id": "llama-pin",
                            "display_name": "llama-pin",
                            "repo_id": "org/llama",
                            "cache_state": "cached",
                            "unique_size_bytes": 2_100_000_000,
                            "nominal_size_bytes": 16_100_000_000,
                        }
                    ],
                    "default_cache": "hf",
                    "skipped": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    app = VelaApp(configs_dir=config_dir, target_client=RemoveConfirmClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager", "model manager did not open"
        )
        await pilot.press("x")
        await _wait_for_condition(
            lambda: app.screen.id == "confirm", "remove confirm did not open"
        )
        message = str(app.screen.query_one("#confirm-message", Static).content)
        assert "cannot be undone" in message
        assert "2.1 GB" in message


@pytest.mark.asyncio
async def test_pin_download_now_strips_flag_and_kicks_download(config_dir: Path) -> None:
    # J15: download_now never reaches the pin RPC; it kicks the existing
    # download job after a successful pin, with an honest bridge message.
    class PinDownloadClient:
        connected = False

        def __init__(self) -> None:
            self.pin_params: dict | None = None
            self.download_params: dict | None = None
            self.events = _BridgeJobEvents({"ok": True, "detail": "model cached"})

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "pin_model":
                self.pin_params = dict(params)
                return {
                    "entry": {"entry_id": "qwen-pin", "display_name": "qwen-pin"},
                    "warnings": [],
                }
            if method == "download_model":
                self.download_params = dict(params)
                self.events.arm(str(params["job_id"]))
                return {"job_id": params["job_id"], "kind": "download_model", "status": "running"}
            if method == "list_models":
                return {
                    "models": [
                        {"entry_id": "qwen-pin", "display_name": "qwen-pin",
                         "cache_state": "remote_only"}
                    ],
                    "default_cache": "hf",
                    "skipped": [],
                }
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            return self.events

    client = PinDownloadClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        app._handle_pin_model_submission(
            {"repo_id": "Qwen/Qwen3-32B", "download_now": True}
        )
        await _wait_for_condition(
            lambda: client.download_params is not None,
            "download job was not kicked after pin",
        )
        assert client.pin_params is not None
        assert "download_now" not in client.pin_params
        assert client.download_params["model_ref"] == "qwen-pin"
        bridge = [note for note in notifications if "Pinned & downloading" in note]
        assert bridge and "qwen-pin" in bridge[0]


@pytest.mark.asyncio
async def test_palette_clone_deployment_prefills_wizard(config_dir: Path) -> None:
    # J26: the "new variant in 30 seconds" flow — clone an existing config
    # into a prefilled wizard with a suggested name.
    class CloneComposerClient(_JourneyComposerClient):
        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": str(config_dir / "alpha.yaml"),
                            "name": "alpha",
                            "model": "org/alpha",
                            "target": "local",
                            "warnings": [],
                            "config": {
                                "name": "alpha",
                                "model": "org/alpha",
                                "server": {"host": "127.0.0.1", "port": 8101},
                            },
                        }
                    ],
                    "invalid": [],
                }
            return await super().call(method, params)

    app = VelaApp(configs_dir=config_dir, target_client=CloneComposerClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        commands = list(app.get_system_commands(app.screen))
        clone = next(
            command for command in commands if command.title == "Clone deployment: alpha"
        )
        clone.callback()
        await _wait_for_condition(
            lambda: app.screen.id == "new-deployment"
            and bool(app.screen.query(Input))
            and app.screen.query_one("#new-deployment-name", Input).value == "alpha-2",
            "clone did not open the wizard prefilled",
        )
        assert app.screen.query_one("#new-deployment-model", Input).value == "org/alpha"
        # Port intentionally blank — auto-allocation avoids cloning a collision.
        assert app.screen.query_one("#new-deployment-port", Input).value == ""


@pytest.mark.asyncio
async def test_preflight_banner_lists_all_failures(config_dir: Path) -> None:
    # J29: the operator sees the whole checklist, not just the first failure.
    write_yaml(
        config_dir / "alpha.yaml",
        """
        name: alpha
        model: org/alpha
        """,
    )
    app = VelaApp(configs_dir=config_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        ok = app._handle_preflight_result(
            {
                "ok": False,
                "failures": [
                    {"kind": "PORT_IN_USE", "detail": "port 8101 is already bound"},
                    {"kind": "MODEL_NOT_FOUND", "detail": "model weights missing"},
                ],
            }
        )
        assert ok is False
        assert "port 8101 is already bound" in app.error_text
        assert "model weights missing" in app.error_text


@pytest.mark.asyncio
async def test_remove_build_refusal_names_blocking_configs(config_dir: Path) -> None:
    # J34: the refusal tells the user WHICH configs pin the build.
    class RefusingClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "remove_build":
                raise TargetCallError(
                    "resource-in-use",
                    "build is pinned by one or more configs",
                    {"build": "b1", "configs": ["alpha", "beta"]},
                )
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    app = VelaApp(configs_dir=config_dir, target_client=RefusingClient())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._remove_build("b1", "stable-cu124")
        assert "pinned by: alpha, beta" in app.error_text


@pytest.mark.asyncio
async def test_config_picker_offers_push_affordance(config_dir: Path) -> None:
    # J36: the picker routes "push this config" into the target manager flow.
    write_yaml(
        config_dir / "alpha.yaml",
        """
        name: alpha
        model: org/alpha
        """,
    )
    app = VelaApp(configs_dir=config_dir)
    notifications: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        await pilot.press("c")
        await _wait_for_condition(
            lambda: app.screen.id == "config-picker", "picker did not open"
        )
        await pilot.press("ctrl+t")
        await _wait_for_condition(
            lambda: app.screen.id == "target-manager",
            "push affordance did not open the target manager",
        )
        assert app.current_config is not None and app.current_config.name == "alpha"
        assert any("p pushes" in note for note in notifications)


@pytest.mark.asyncio
async def test_verify_build_reopens_manager_focused(config_dir: Path) -> None:
    # J30: maintenance actions land the user back in the refreshed manager.
    class VerifyClient:
        connected = False

        def __init__(self) -> None:
            self.list_builds_calls = 0

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_builds":
                self.list_builds_calls += 1
                return {
                    "builds": [
                        {"build_id": "01STABLE", "label": "stable-cu124",
                         "status": "ready", "default": True},
                        {"build_id": "01NIGHT", "label": "nightly-cu130",
                         "status": "ready"},
                    ],
                    "skipped": [],
                }
            if method == "verify_build":
                return {"label": "nightly-cu130", "status": "ready", "ok": True}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    client = VerifyClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager", "manager did not open"
        )
        await pilot.press("down")
        await pilot.press("v")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager" and client.list_builds_calls >= 2,
            "manager did not reopen after verify",
        )
        # Reopened focused on the verified build.
        assert app.screen.selected_index == 1


@pytest.mark.asyncio
async def test_verify_model_reopens_manager_focused(config_dir: Path) -> None:
    class ModelVerifyClient:
        connected = False

        def __init__(self) -> None:
            self.list_models_calls = 0

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_models":
                self.list_models_calls += 1
                return {
                    "models": [
                        {"entry_id": "llama-pin", "display_name": "llama-pin",
                         "model_ref": "llama-pin", "cache_state": "cached"},
                    ],
                    "default_cache": "hf",
                    "skipped": [],
                }
            if method == "verify_model":
                return {"cache_state": "verified", "ok": True}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    client = ModelVerifyClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("m")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager", "model manager did not open"
        )
        await pilot.press("v")
        await _wait_for_condition(
            lambda: app.screen.id == "model-manager" and client.list_models_calls >= 2,
            "model manager did not reopen after verify",
        )


@pytest.mark.asyncio
async def test_build_manager_pins_build_to_current_config(config_dir: Path) -> None:
    # J31: P pins the highlighted build to the selected config (toggle).
    write_yaml(
        config_dir / "alpha.yaml",
        """
        name: alpha
        model: org/alpha
        """,
    )
    class PinBuildClient:
        connected = False

        def __init__(self) -> None:
            self.set_calls: list[dict] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {
                    "valid": [
                        {
                            "path": str(config_dir / "alpha.yaml"),
                            "name": "alpha",
                            "model": "org/alpha",
                            "target": "local",
                            "warnings": [],
                            "config": {"name": "alpha", "model": "org/alpha"},
                        }
                    ],
                    "invalid": [],
                }
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/alpha", "warnings": []}
            if method == "list_builds":
                return {
                    "builds": [
                        {"build_id": "01NIGHT", "label": "nightly-cu130",
                         "status": "ready", "default": True}
                    ],
                    "skipped": [],
                }
            if method == "set_config_build":
                self.set_calls.append(dict(params))
                return {"name": params["name"], "build": params["build"]}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, *_args, **_kwargs):
            async def _empty():
                while False:
                    yield {}

            return _empty()

    client = PinBuildClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)
    notifications: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        app.select_config("alpha")
        await pilot.pause()
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager", "manager did not open"
        )
        await pilot.press("P")
        await _wait_for_condition(
            lambda: bool(client.set_calls), "pin was not requested"
        )
        assert client.set_calls[0]["name"] == "alpha"
        assert client.set_calls[0]["build"] == "nightly-cu130"
        assert any("Pinned build nightly-cu130 to alpha" in note for note in notifications)
        # The manager reopens refreshed.
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager", "manager did not reopen after pin"
        )


@pytest.mark.asyncio
async def test_install_uv_from_create_build_unlocks_nightly(config_dir: Path) -> None:
    # J37: the uv-block state offers a one-key install that reopens the form
    # with uv available and the typed values preserved.
    class UvClient:
        connected = False

        def __init__(self) -> None:
            self.events = _BridgeJobEvents({"ok": True, "detail": "uv installed"})
            self.installed = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "list_configs":
                return {"valid": [], "invalid": []}
            if method == "list_builds":
                return {"builds": [], "skipped": []}
            if method == "check_build_prerequisites":
                return {
                    "ok": True,
                    "method": params["method"],
                    "uv_available": self.installed,
                }
            if method == "install_uv":
                self.installed = True
                self.events.arm(str(params["job_id"]))
                return {"job_id": params["job_id"], "kind": "install_uv", "status": "running"}
            if method in {"gpu", "sample_gpus"}:
                return {"samples": [], "note": "GPU stats unavailable", "unavailable": True}
            if method == "discover_runs":
                return {"runs": []}
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            return self.events

    client = UvClient()
    app = VelaApp(configs_dir=config_dir, target_client=client)
    notifications: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, *a, **k: notifications.append(str(message))
        await pilot.press("b")
        await _wait_for_condition(
            lambda: app.screen.id == "build-manager", "manager did not open"
        )
        await pilot.press("n")
        await _wait_for_condition(
            lambda: app.screen.id == "create-build" and bool(app.screen.query(Input)),
            "create build did not open",
        )
        assert "uv not found" in str(
            app.screen.query_one("#create-build-uv-note", Static).content
        )
        app.screen.query_one("#create-build-label", Input).value = "nightly-cu130"
        await pilot.press("ctrl+g")
        await _wait_for_condition(
            lambda: app.screen.id == "create-build"
            and bool(app.screen.query(Input))
            and "uv available" in str(
                app.screen.query_one("#create-build-uv-note", Static).content
            ),
            "form did not reopen with uv available",
        )
        # Typed values survive the install round-trip.
        assert app.screen.query_one("#create-build-label", Input).value == "nightly-cu130"
        assert any("uv installed" in note for note in notifications)
