from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import typer
from conftest import write_yaml
from typer.testing import CliRunner

from vela import __version__
from vela import cli as cli_module
from vela.agent.auth import configured_agent_token, default_agent_token_file
from vela.agent.local import TargetCallError
from vela.cli import _enable_textual_debug_features
from vela.config.targets import TargetConfig, TransportKind, load_targets_file
from vela.engine import process_manager as process_manager_module
from vela.engine import supervisor as supervisor_module
from vela.engine.phases import Phase
from vela.engine.sidecar import verify_sidecar_from_system
from vela.engine.supervisor import run_supervisor
from vela.tui.app import VelaApp


def test_debug_mode_enables_textual_debug_and_devtools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXTUAL", "foo,debug")

    _enable_textual_debug_features()

    assert os.environ["TEXTUAL"] == "debug,devtools,foo"


def test_every_cli_command_and_group_self_documents() -> None:
    # 7.1: every command and every sub-app must carry non-empty help so `vela --help`
    # (and each `vela <group> --help`) reads as a product surface, not a blank list.
    missing_commands: list[str] = []
    missing_groups: list[str] = []

    def walk(group: typer.Typer, prefix: str) -> None:
        for command in group.registered_commands:
            name = command.name or (command.callback.__name__ if command.callback else "?")
            help_text = command.help or (command.callback.__doc__ if command.callback else None)
            if not (help_text and help_text.strip()):
                missing_commands.append(f"{prefix}{name}")
        for sub in group.registered_groups:
            sub_name = sub.name or "?"
            sub_app = sub.typer_instance
            sub_help = sub.help or (sub_app.info.help if sub_app is not None else None)
            if not (sub_help and sub_help.strip()):
                missing_groups.append(f"{prefix}{sub_name}")
            if sub_app is not None:
                walk(sub_app, prefix=f"{prefix}{sub_name} ")

    walk(cli_module.app, prefix="")

    assert missing_commands == [], f"commands missing help: {missing_commands}"
    assert missing_groups == [], f"sub-apps missing help: {missing_groups}"


def test_cli_root_version_option_prints_version_without_launching_tui() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "vela.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == __version__
    assert proc.stderr == ""


def test_cli_agent_gen_token_prints_strong_urlsafe_token() -> None:
    result = CliRunner().invoke(cli_module.app, ["agent", "gen-token"])

    assert result.exit_code == 0, result.output
    token = result.output.strip()
    assert len(token) >= 43
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)


def test_cli_agent_gen_token_install_writes_default_token_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VELA_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("VELA_AGENT_TOKEN_FILE", raising=False)

    result = CliRunner().invoke(cli_module.app, ["agent", "gen-token", "--install"])

    assert result.exit_code == 0, result.output
    token_path = default_agent_token_file()
    assert f"installed agent token\t{token_path}" in result.output
    token = token_path.read_text(encoding="utf-8").strip()
    assert len(token) >= 43
    assert configured_agent_token() == token
    assert (token_path.stat().st_mode & 0o777) == 0o600


def test_cli_agent_gen_token_install_target_writes_local_and_remote_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VELA_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("VELA_AGENT_TOKEN_FILE", raising=False)
    token = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@fake",
        agent_command=["vela", "agent", "connect"],
    )
    client_calls: list[tuple[str, dict[str, str]]] = []

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [TargetConfig(name="local"), blackbird]

        def by_name(self, name: str) -> TargetConfig:
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    class FakeTargetClient:
        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def call(self, method: str, params):
            client_calls.append((method, params))
            if method == "write_agent_token":
                return {
                    "path": "/home/bgconley/.config/vela/agent-token",
                    "mode": "0600",
                }
            raise AssertionError(f"unexpected target client call: {method}")

    monkeypatch.setattr(cli_module, "generate_agent_token", lambda _nbytes: token)
    monkeypatch.setattr(
        cli_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target: FakeTargetClient(),
    )

    result = CliRunner().invoke(
        cli_module.app,
        ["agent", "gen-token", "--install", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    local_path = default_agent_token_file()
    assert local_path.read_text(encoding="utf-8").strip() == token
    assert (local_path.stat().st_mode & 0o777) == 0o600
    assert client_calls == [("write_agent_token", {"token": token})]
    assert f"installed agent token\t{local_path}" in result.output
    assert (
        "installed target agent token\tblackbird\t"
        "/home/bgconley/.config/vela/agent-token"
    ) in result.output


def test_cli_root_target_option_launches_tui_with_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, object]] = []
    run_calls: list[str] = []

    class FakeTui:
        def __init__(self, **kwargs) -> None:
            constructed.append(kwargs)

        def run(self) -> None:
            run_calls.append("run")

    monkeypatch.setattr(cli_module, "VelaApp", FakeTui)

    result = CliRunner().invoke(cli_module.app, ["--target", "blackbird"])

    assert result.exit_code == 0, result.output
    assert constructed == [
        {
            "configs_dir": None,
            "debug_log_path": None,
            "target_name": "blackbird",
        }
    ]
    assert run_calls == ["run"]


def test_cli_tui_alias_launches_tui_with_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, object]] = []
    run_calls: list[str] = []

    class FakeTui:
        def __init__(self, **kwargs) -> None:
            constructed.append(kwargs)

        def run(self) -> None:
            run_calls.append("run")

    monkeypatch.setattr(cli_module, "VelaApp", FakeTui)

    result = CliRunner().invoke(cli_module.app, ["tui", "--target", "blackbird"])

    assert result.exit_code == 0, result.output
    assert constructed == [
        {
            "configs_dir": None,
            "debug_log_path": None,
            "target_name": "blackbird",
        }
    ]
    assert run_calls == ["run"]


def test_cli_list_uses_local_target_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_agent_call(method: str, params: dict[str, str], *, target_name: str = "local"):
        assert target_name == "local"
        calls.append((method, params))
        return {
            "valid": [{"name": "agent-cfg", "model": "org/model"}],
            "invalid": [{"path": "bad.yaml", "errors": ["broken"]}],
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    cli_module.list_configs(configs_dir=tmp_path)

    stdout, stderr = capsys.readouterr()
    assert calls == [("list_configs", {"configs_dir": str(tmp_path)})]
    assert "agent-cfg\torg/model" in stdout
    assert "INVALID bad.yaml\tbroken" in stdout
    assert stderr == ""


def test_cli_preview_uses_local_target_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_agent_call(method: str, params: dict[str, str], *, target_name: str = "local"):
        assert target_name == "local"
        calls.append((method, params))
        return {"preview": "agent-preview", "warnings": ["heads up"]}

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    cli_module.preview("agent-cfg", configs_dir=tmp_path)

    stdout, stderr = capsys.readouterr()
    assert calls == [("preview", {"name": "agent-cfg", "configs_dir": str(tmp_path)})]
    assert stdout == "agent-preview\n"
    assert stderr == "WARNING: heads up\n"


class _FakeRequireCachedClient:
    def __init__(self, launch_warnings: list[dict[str, object]]) -> None:
        self.prepare_calls: list[dict[str, object]] = []
        self._launch_warnings = launch_warnings

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def call(self, method: str, params):
        if method == "prepare_launch":
            self.prepare_calls.append(dict(params or {}))
            return {
                "config": {"name": "cfg", "model": "org/model"},
                "build": {"warnings": []},
                "preflight": None,
                "launch_warnings": self._launch_warnings,
            }
        raise AssertionError(f"unexpected target client call: {method}")


def test_cli_smoke_require_cached_flag_threads_and_echoes_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRequireCachedClient(
        [
            {
                "kind": "model-not-cached",
                "entry_id": "01CACHE",
                "detail": "model cache-llama (01CACHE) is not cached",
            }
        ]
    )

    async def fake_smoke_config_cli(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(
        cli_module, "_target_client_for_name_or_exit", lambda target_name: client
    )
    monkeypatch.setattr(cli_module, "_smoke_config_cli", fake_smoke_config_cli)

    result = CliRunner().invoke(cli_module.app, ["smoke", "cfg", "--require-cached"])

    assert result.exit_code == 0, result.output
    assert client.prepare_calls and client.prepare_calls[0].get("require_cached") == "true"
    assert "WARNING:" in result.output
    assert "not cached" in result.output


def test_cli_smoke_without_require_cached_omits_the_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeRequireCachedClient([])

    async def fake_smoke_config_cli(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(
        cli_module, "_target_client_for_name_or_exit", lambda target_name: client
    )
    monkeypatch.setattr(cli_module, "_smoke_config_cli", fake_smoke_config_cli)

    result = CliRunner().invoke(cli_module.app, ["smoke", "cfg"])

    assert result.exit_code == 0, result.output
    assert client.prepare_calls and "require_cached" not in client.prepare_calls[0]
    assert "WARNING:" not in result.output


def test_cli_preview_target_option_uses_selected_target_from_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        agent_command=["vela", "agent", "connect"],
    )
    requested_target_names: list[str] = []
    requested_targets: list[TargetConfig] = []
    client_calls: list[tuple[str, dict[str, str]]] = []

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [TargetConfig(name="local"), blackbird]

        def by_name(self, name: str) -> TargetConfig:
            requested_target_names.append(name)
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    class FakeTargetClient:
        connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            client_calls.append((method, params))
            if method == "preview":
                return {"preview": "remote-preview", "warnings": []}
            raise AssertionError(f"unexpected target client call: {method}")

    def fake_target_client_for_config(target):
        requested_targets.append(target)
        return FakeTargetClient()

    monkeypatch.setattr(
        cli_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(cli_module, "target_client_for_config", fake_target_client_for_config)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "preview",
            "remote-cfg",
            "--configs-dir",
            str(tmp_path),
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "remote-preview\n"
    assert requested_target_names == ["blackbird"]
    assert requested_targets == [blackbird]
    assert client_calls == [
        ("preview", {"name": "remote-cfg", "configs_dir": str(tmp_path)})
    ]


def test_cli_preview_passes_build_model_revision_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str], str]] = []

    def fake_agent_call(method: str, params: dict[str, str], *, target_name: str = "local"):
        calls.append((method, params, target_name))
        return {"preview": "override-preview", "warnings": []}

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "preview",
            "remote-cfg",
            "--configs-dir",
            str(tmp_path),
            "--target",
            "blackbird",
            "--build-id",
            "01BUILD",
            "--model-ref",
            "01MODEL",
            "--revision",
            "abc123",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "override-preview\n"
    assert calls == [
        (
            "preview",
            {
                "name": "remote-cfg",
                "configs_dir": str(tmp_path),
                "build_id": "01BUILD",
                "model_ref": "01MODEL",
                "revision": "abc123",
            },
            "blackbird",
        )
    ]


def test_cli_build_list_uses_selected_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "builds": [
                {
                    "build_id": "01BUILD",
                    "label": "nightly-cu130",
                    "status": "ready",
                    "default": True,
                }
            ],
            "default_build_id": "01BUILD",
            "skipped": [],
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["build", "list", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("list_builds", None, "blackbird")]
    assert result.output.splitlines() == [
        "*\t01BUILD\tnightly-cu130\tready",
    ]


def test_cli_build_list_json_outputs_agent_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "builds": [
            {
                "build_id": "01BUILD",
                "label": "nightly-cu130",
                "status": "ready",
                "default": True,
            }
        ],
        "default_build_id": "01BUILD",
        "skipped": [],
    }

    monkeypatch.setattr(
        cli_module,
        "_agent_call",
        lambda method, params=None, *, target_name="local": payload,
    )

    result = CliRunner().invoke(cli_module.app, ["build", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == payload


def test_cli_build_add_streams_job_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeUuid:
        hex = "job-build-1"

    class FakeEvents:
        def __init__(self) -> None:
            self._events = iter(
                [
                    {
                        "event": "job_progress",
                        "job_id": "job-build-1",
                        "kind": "committed",
                        "text": "Installing build",
                        "level": "INFO",
                    },
                    {
                        "event": "job_done",
                        "job_id": "job-build-1",
                        "ok": True,
                        "detail": "build ready",
                    },
                ]
            )
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self) -> None:
            self.closed = True

    class FakeTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []
            self.events = FakeEvents()

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "check_build_prerequisites":
                return {"ok": True, "method": params["method"], "uv_available": True}
            if method == "create_build":
                return {
                    "job_id": params["job_id"],
                    "kind": "create_build",
                    "status": "running",
                }
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            self.subscribe_calls.append((run_ids, resume_from))
            return self.events

    target_client = FakeTargetClient()
    monkeypatch.setattr(cli_module.uuid, "uuid4", lambda: FakeUuid())
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target_name: target_client,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "build",
            "add",
            "--method",
            "nightly",
            "--channel",
            "cu130",
            "--label",
            "nvfp4",
            "--python",
            "3.12",
            "--env",
            "TORCH_CUDA_ARCH_LIST=10.0",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert target_client.subscribe_calls == [(["job-build-1"], "live")]
    assert target_client.calls == [
        (
            "check_build_prerequisites",
            {
                "method": "nightly",
                "channel": "cu130",
                "label": "nvfp4",
                "python": "3.12",
                "env": ["TORCH_CUDA_ARCH_LIST=10.0"],
            },
        ),
        (
            "create_build",
            {
                "job_id": "job-build-1",
                "method": "nightly",
                "channel": "cu130",
                "label": "nvfp4",
                "python": "3.12",
                "env": ["TORCH_CUDA_ARCH_LIST=10.0"],
            },
        )
    ]
    assert target_client.events.closed is True
    assert result.output.splitlines() == [
        "Installing build",
        "DONE\tjob-build-1\tbuild ready",
    ]


def test_cli_build_add_help_surfaces_uv_requirement() -> None:
    result = CliRunner().invoke(cli_module.app, ["build", "add", "--help"])

    assert result.exit_code == 0, result.output
    assert "nightly/commit require" in result.output
    assert "uv on the target" in result.output


def test_cli_build_doctor_reports_uv_required_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        requested_method = str((params or {}).get("method") or "")
        if requested_method in {"nightly", "commit"}:
            raise cli_module.TargetCallError(
                "feature-unavailable",
                f"create_build method={requested_method} requires uv",
                {"reason": "uv-required", "method": requested_method},
            )
        return {"ok": True, "method": requested_method, "uv_available": False}

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["build", "doctor", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == len(cli_module.BUILD_DOCTOR_METHODS)
    assert all(call[2] == "blackbird" for call in calls)
    assert "build doctor\tblackbird" in result.output
    assert "uv\tmissing" in result.output
    assert "nightly\tblocked\tuv-required" in result.output
    assert "commit\tblocked\tuv-required" in result.output
    assert "vela build doctor --target blackbird" in result.output


def test_cli_build_add_rejects_uv_less_target_before_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTargetClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "check_build_prerequisites":
                raise cli_module.TargetCallError(
                    "feature-unavailable",
                    "create_build method=nightly requires uv",
                    {"reason": "uv-required", "method": "nightly"},
                )
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            self.subscribe_calls.append((run_ids, resume_from))
            raise AssertionError("build job should not be subscribed before uv precheck")

    target_client = FakeTargetClient()
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target_name: target_client,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "build",
            "add",
            "--method",
            "nightly",
            "--channel",
            "cu130",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 2
    assert target_client.calls == [
        (
            "check_build_prerequisites",
            {"method": "nightly", "channel": "cu130"},
        )
    ]
    assert target_client.subscribe_calls == []
    assert "ERROR UV_REQUIRED: create_build method=nightly requires uv" in result.output
    assert "vela build doctor --target blackbird" in result.output


def test_cli_build_add_git_passes_precompiled_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeUuid:
        hex = "job-build-git"

    class FakeEvents:
        def __init__(self) -> None:
            self.closed = False
            self._events = iter(
                [
                    {
                        "event": "job_done",
                        "job_id": "job-build-git",
                        "ok": True,
                        "detail": "build ready",
                    }
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self) -> None:
            self.closed = True

    class FakeTargetClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.events = FakeEvents()

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "check_build_prerequisites":
                return {"ok": True, "method": params["method"], "uv_available": False}
            return {
                "job_id": params["job_id"],
                "kind": "create_build",
                "status": "running",
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            return self.events

    target_client = FakeTargetClient()
    monkeypatch.setattr(cli_module.uuid, "uuid4", lambda: FakeUuid())
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target_name: target_client,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "build",
            "add",
            "--method",
            "git",
            "--url",
            "https://github.com/vllm-project/vllm.git",
            "--ref",
            "main",
            "--precompiled",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert target_client.calls == [
        (
            "check_build_prerequisites",
            {
                "method": "git",
                "url": "https://github.com/vllm-project/vllm.git",
                "ref": "main",
                "precompiled": "true",
            },
        ),
        (
            "create_build",
            {
                "job_id": "job-build-git",
                "method": "git",
                "url": "https://github.com/vllm-project/vllm.git",
                "ref": "main",
                "precompiled": "true",
            },
        )
    ]


def test_cli_build_inspect_prints_manifest_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "manifest": {
                "build_id": "01BUILD",
                "label": "nightly-cu130",
                "status": "ready",
                "resolved": {"vllm": "0.17.0.dev", "cuda": "13.0"},
                "paths": {"root": "/agent/builds/01BUILD"},
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["build", "inspect", "nightly-cu130", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("inspect_build", {"build": "nightly-cu130"}, "blackbird")]
    assert result.output.splitlines() == [
        "build_id\t01BUILD",
        "label\tnightly-cu130",
        "status\tready",
        'resolved\t{"cuda": "13.0", "vllm": "0.17.0.dev"}',
        'paths\t{"root": "/agent/builds/01BUILD"}',
    ]


def test_cli_build_inspect_error_uses_target_string_for_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        assert method == "inspect_build"
        assert params == {"build": "nightly-cu130"}
        assert target_name == "blackbird"
        raise cli_module.TargetCallError(
            "agent-unreachable",
            "SSH target agent bridge failed",
            {"reason": "ssh-auth", "stderr": "Permission denied (publickey)."},
        )

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["build", "inspect", "nightly-cu130", "--target", "blackbird"],
    )

    assert result.exit_code == 2
    assert "ERROR AGENT_UNREACHABLE" in result.output
    assert "Permission denied (publickey)." in result.output
    assert "vela targets setup-ssh blackbird" in result.output


def test_cli_build_adopt_passes_external_venv_to_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []
    venv_dir = tmp_path / "venv"

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "build_id": "01ADOPTED",
            "label": "external-nightly",
            "status": "adopted",
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "build",
            "adopt",
            str(venv_dir),
            "--build-id",
            "01ADOPTED",
            "--label",
            "external-nightly",
            "--vllm-version",
            "0.17.0.dev",
            "--vllm-version-profile",
            "current",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "adopt_build",
            {
                "label": "external-nightly",
                "venv_path": str(venv_dir),
                "vllm_version": "0.17.0.dev",
                "vllm_version_profile": "current",
            },
            "blackbird",
        )
    ]
    assert result.output == "adopted build\t01ADOPTED\texternal-nightly\n"


def test_cli_build_adopt_copy_passes_copy_flag_to_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []
    venv_dir = tmp_path / "venv"

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "build_id": "01ADOPTED",
            "label": "external-nightly",
            "status": "adopted",
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "build",
            "adopt",
            str(venv_dir),
            "--label",
            "external-nightly",
            "--copy",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "adopt_build",
            {
                "label": "external-nightly",
                "venv_path": str(venv_dir),
                "copy": "true",
            },
            "blackbird",
        )
    ]
    assert result.output == "adopted build\t01ADOPTED\texternal-nightly\n"


def test_cli_build_adopt_allows_agent_generated_build_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []
    venv_dir = tmp_path / "venv"

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "build_id": "01J9Z8KQ4M7R2VEXAMPLE0001",
            "label": "external-nightly",
            "status": "adopted",
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "build",
            "adopt",
            str(venv_dir),
            "--label",
            "external-nightly",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "adopt_build",
            {
                "label": "external-nightly",
                "venv_path": str(venv_dir),
            },
            "blackbird",
        )
    ]


def test_cli_build_select_uses_selected_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {"build_id": "01BUILD", "label": "nightly-cu130", "active": True}

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["build", "select", "nightly-cu130", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("select_build", {"build": "nightly-cu130"}, "blackbird")]
    assert result.output == "selected build\t01BUILD\tnightly-cu130\n"


def test_cli_build_verify_prints_agent_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "build_id": "01BUILD",
            "ok": True,
            "status": "ready",
            "detail": "build verified",
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["build", "verify", "01BUILD", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("verify_build", {"build": "01BUILD"}, "blackbird")]
    assert result.output == "OK\t01BUILD\tready\tbuild verified\n"


def test_cli_build_verify_exits_nonzero_on_failed_agent_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        del method, params, target_name
        return {
            "build_id": "01BROKEN",
            "ok": False,
            "status": "broken",
            "detail": "build verification failed: pip-freeze-probe-failed",
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(cli_module.app, ["build", "verify", "01BROKEN"])

    assert result.exit_code == 2
    assert (
        result.output
        == "FAIL\t01BROKEN\tbroken\tbuild verification failed: pip-freeze-probe-failed\n"
    )


def test_cli_build_run_streams_target_local_build_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeUuid:
        hex = "job-build-run"

    class FakeEvents:
        def __init__(self) -> None:
            self._events = iter(
                [
                    {
                        "event": "job_progress",
                        "job_id": "job-build-run",
                        "kind": "committed",
                        "text": "Serving org/model",
                        "level": "INFO",
                    },
                    {
                        "event": "job_done",
                        "job_id": "job-build-run",
                        "ok": True,
                        "detail": "build command exited 0",
                    },
                ]
            )
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self) -> None:
            self.closed = True

    class FakeTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []
            self.events = FakeEvents()

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "run_build":
                return {
                    "job_id": params["job_id"],
                    "kind": "run_build",
                    "status": "running",
                }
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            self.subscribe_calls.append((run_ids, resume_from))
            return self.events

    target_client = FakeTargetClient()
    monkeypatch.setattr(cli_module.uuid, "uuid4", lambda: FakeUuid())
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target_name: target_client,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "build",
            "run",
            "nightly-cu130",
            "--target",
            "blackbird",
            "--",
            "serve",
            "org/model",
            "--port",
            "8000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert target_client.subscribe_calls == [(["job-build-run"], "live")]
    assert target_client.calls == [
        (
            "run_build",
            {
                "job_id": "job-build-run",
                "build": "nightly-cu130",
                "argv": ["serve", "org/model", "--port", "8000"],
            },
        )
    ]
    assert target_client.events.closed is True
    assert result.output.splitlines() == [
        "Serving org/model",
        "DONE\tjob-build-run\tbuild command exited 0",
    ]


def test_cli_build_repair_prints_agent_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "build_id": "01BUILD",
            "ok": True,
            "status": "ready",
            "detail": "build repaired",
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["build", "repair", "01BUILD", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("repair_build", {"build": "01BUILD"}, "blackbird")]
    assert result.output == "OK\t01BUILD\tready\tbuild repaired\n"


def test_cli_build_remove_requires_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    monkeypatch.setattr(
        cli_module,
        "_agent_call",
        lambda method, params=None, *, target_name="local": calls.append(
            (method, params, target_name)
        ),
    )

    result = CliRunner().invoke(cli_module.app, ["build", "remove", "01BUILD"])

    assert result.exit_code == 2
    assert calls == []
    assert "use --yes to remove a build" in result.output


def test_cli_build_remove_passes_configs_dir_to_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "build_id": "01BUILD",
            "label": "nightly-cu130",
            "removed": True,
            "removed_path": "/agent/builds/01BUILD",
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "build",
            "remove",
            "nightly-cu130",
            "--target",
            "blackbird",
            "--configs-dir",
            str(tmp_path),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "remove_build",
            {"build": "nightly-cu130", "configs_dir": str(tmp_path)},
            "blackbird",
        )
    ]
    assert result.output == "removed build\t01BUILD\tnightly-cu130\n"


def test_cli_model_list_uses_selected_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "models": [
                {
                    "entry_id": "01MODEL",
                    "display_name": "llama-pin",
                    "source": "hf_repo",
                    "cache_state": "cached",
                }
            ],
            "default_cache": "hf",
            "app_download_dir": None,
            "skipped": [],
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "list", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("list_models", None, "blackbird")]
    assert result.output.splitlines() == [
        "01MODEL\tllama-pin\thf_repo\tcached",
    ]


def test_cli_model_list_passes_cache_and_pin_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "models": [
                {
                    "entry_id": "01PINNED",
                    "display_name": "llama-pin",
                    "source": "hf_repo",
                    "cache_state": "cached",
                }
            ],
            "default_cache": "hf",
            "app_download_dir": None,
            "skipped": [],
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "list",
            "--cached-only",
            "--pinned-only",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "list_models",
            {"cached_only": "true", "pinned_only": "true"},
            "blackbird",
        )
    ]
    assert result.output.splitlines() == [
        "01PINNED\tllama-pin\thf_repo\tcached",
    ]


def test_cli_model_refresh_prints_refreshed_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "refreshed": 1,
            "models": [
                {
                    "entry_id": "01MODEL",
                    "display_name": "llama-pin",
                    "source": "hf_repo",
                    "cache_state": "cached",
                }
            ],
            "skipped": [],
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "refresh", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("refresh_models", None, "blackbird")]
    assert result.output.splitlines() == [
        "refreshed models\t1",
        "01MODEL\tllama-pin\thf_repo\tcached",
    ]


def test_cli_model_inspect_prints_entry_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01MODEL",
                "display_name": "llama-pin",
                "source": "hf_repo",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
                "commit_sha": "abc123",
                "cache_state": "cached",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "inspect", "llama-pin", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("inspect_model", {"model_ref": "llama-pin"}, "blackbird"),
    ]
    assert result.output.splitlines() == [
        "entry_id\t01MODEL",
        "display_name\tllama-pin",
        "source\thf_repo",
        "repo_id\tmeta-llama/Llama-3.1-8B-Instruct",
        "revision\tmain",
        "commit_sha\tabc123",
        "cache_state\tcached",
    ]


def test_cli_model_inspect_shows_last_download_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 6c (bug-289 item d): text `model inspect` must surface last_download_* so an
    # operator can see the cache holds a revision other than the pinned one.
    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        return {
            "entry": {
                "entry_id": "01MODEL",
                "display_name": "llama-pin",
                "source": "hf_repo",
                "repo_id": "org/repo",
                "revision": "main",
                "commit_sha": "abc123",
                "last_download_revision": "v2.0",
                "last_download_sha": "def456",
                "cache_state": "cached",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(cli_module.app, ["model", "inspect", "llama-pin"])

    assert result.exit_code == 0, result.output
    assert "last_download_revision\tv2.0" in result.output.splitlines()
    assert "last_download_sha\tdef456" in result.output.splitlines()


def test_cli_model_inspect_shows_validated_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Phase-5 follow-up: text `model inspect` must surface validated when the pin
    # was taken on faith (--offline / a network failure at pin time). Only the
    # False value is ever recorded on the entry, so that is what must render;
    # docs/builds-and-models.md already claims this field is shown.
    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        return {
            "entry": {
                "entry_id": "01MODEL",
                "display_name": "llama-pin",
                "source": "hf_repo",
                "repo_id": "org/repo",
                "commit_sha": "abc123",
                "validated": False,
                "cache_state": "remote_only",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(cli_module.app, ["model", "inspect", "llama-pin"])

    assert result.exit_code == 0, result.output
    assert "validated\tFalse" in result.output.splitlines()


def test_cli_warns_once_on_stale_local_daemon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # bug-238: first contact with a stale LOCAL socket daemon prints the restart
    # banner exactly once per process; the wording is stable and pinned.
    from vela.transport.socket import UnixSocketTargetClient

    monkeypatch.setattr(cli_module, "_stale_local_daemon_warned", False)
    client = UnixSocketTargetClient("/tmp/vela-stale-test.sock")
    stale = {"agent_version": "0.0.1", "daemon_start_ts": "2026-06-09T00:00:00Z"}

    cli_module._maybe_warn_stale_local_daemon(client, stale)
    cli_module._maybe_warn_stale_local_daemon(client, stale)

    err = capsys.readouterr().err
    assert err.count("local daemon is running vela 0.0.1 (started 2026-06-09)") == 1
    assert "restart with: vela agent restart" in err


def test_cli_stale_daemon_banner_skips_non_socket_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An in-process (or SSH) client is never the stale local daemon — no banner.
    from vela.agent.local import LocalAgent
    from vela.transport.inprocess import InProcessTargetClient

    monkeypatch.setattr(cli_module, "_stale_local_daemon_warned", False)
    client = InProcessTargetClient(LocalAgent())
    stale = {"agent_version": "0.0.1", "daemon_start_ts": "2026-06-09T00:00:00Z"}

    cli_module._maybe_warn_stale_local_daemon(client, stale)

    assert "local daemon is running" not in capsys.readouterr().err


def test_cli_model_adopt_uses_verified_local_pin_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []
    model_dir = tmp_path / "models" / "local-llama"

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01LOCAL",
                "display_name": "local-llama",
                "source": "local_path",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "adopt",
            str(model_dir),
            "--entry-id",
            "01LOCAL",
            "--display-name",
            "local-llama",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "pin_model",
            {
                "display_name": "local-llama",
                "local_path": str(model_dir),
                "source": "local_path",
            },
            "blackbird",
        )
    ]
    assert result.output == "adopted model\t01LOCAL\tlocal-llama\n"


def test_cli_model_adopt_allows_agent_generated_entry_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []
    model_dir = tmp_path / "models" / "local-llama"

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01J9Z9ABCDEF2VEXAMPLEMODEL01",
                "display_name": "local-llama",
                "source": "local_path",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "adopt",
            str(model_dir),
            "--display-name",
            "local-llama",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "pin_model",
            {
                "display_name": "local-llama",
                "local_path": str(model_dir),
                "source": "local_path",
            },
            "blackbird",
        )
    ]


def test_cli_model_adopt_accepts_name_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []
    model_dir = tmp_path / "models" / "local-llama"

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01MODEL",
                "display_name": "local-llama",
                "source": "local_path",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "adopt",
            str(model_dir),
            "--name",
            "local-llama",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "pin_model",
            {
                "display_name": "local-llama",
                "local_path": str(model_dir),
                "source": "local_path",
            },
            "blackbird",
        )
    ]


def test_cli_model_pin_passes_metadata_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01MODEL",
                "display_name": "llama-pin",
                "source": "hf_repo",
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

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "pin",
            "01MODEL",
            "--repo-id",
            "meta-llama/Llama-3.1-8B-Instruct",
            "--display-name",
            "llama-pin",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "pin_model",
            {
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "display_name": "llama-pin",
            },
            "blackbird",
        )
    ]
    assert "pinned model\t01MODEL\tllama-pin\n" in result.output
    assert (
        "WARNING: pinned remote-only model has no immutable commit sha; "
        "launch will be blocked until it is re-pinned online"
    ) in result.output


def test_cli_model_pin_new_flag_threads_new_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01MODEL",
                "display_name": "org/repo",
                "source": "hf_repo",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "pin", "org/repo", "--new"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("pin_model", {"repo_id": "org/repo", "new": "true"}, "local"),
    ]


def test_cli_model_pin_passes_optional_model_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01MODEL",
                "display_name": "llama-pin",
                "source": "hf_repo",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "pin",
            "meta-llama/Llama-3.1-8B-Instruct",
            "--display-name",
            "llama-pin",
            "--quant-format",
            "awq",
            "--tokenizer",
            "meta-llama/Llama-3.1-tokenizer",
            "--gated",
            "--token-required",
            "--notes",
            "license accepted",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "pin_model",
            {
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "display_name": "llama-pin",
                "quant_format": "awq",
                "tokenizer": "meta-llama/Llama-3.1-tokenizer",
                "gated": "true",
                "token_required": "true",
                "notes": "license accepted",
            },
            "blackbird",
        )
    ]


def test_cli_model_add_alias_passes_name_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01MODEL",
                "display_name": "llama-pin",
                "source": "hf_repo",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "add",
            "meta-llama/Llama-3.1-8B-Instruct",
            "--name",
            "llama-pin",
            "--revision",
            "main",
            "--tokenizer",
            "meta-llama/Llama-3.1-tokenizer",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "pin_model",
            {
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "display_name": "llama-pin",
                "revision": "main",
                "tokenizer": "meta-llama/Llama-3.1-tokenizer",
            },
            "blackbird",
        )
    ]


def test_cli_model_pin_uses_repo_argument_and_generated_entry_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01J9Z9ABCDEF2VEXAMPLEMODEL01",
                "display_name": "llama-pin",
                "source": "hf_repo",
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "pin",
            "meta-llama/Llama-3.1-8B-Instruct",
            "--display-name",
            "llama-pin",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "pin_model",
            {
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "display_name": "llama-pin",
            },
            "blackbird",
        )
    ]


def test_cli_model_pin_url_source_passes_url_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []
    model_url = "https://models.example/Qwen/example-q4.gguf"

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry": {
                "entry_id": "01URLMODEL",
                "display_name": "url-gguf",
                "source": "url",
                "url": model_url,
            }
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "pin",
            "url-gguf",
            "--url",
            model_url,
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "pin_model",
            {
                "url": model_url,
                "display_name": "url-gguf",
                "source": "url",
            },
            "blackbird",
        )
    ]
    assert result.output == "pinned model\t01URLMODEL\turl-gguf\n"


def test_cli_model_verify_prints_agent_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry_id": "01MODEL",
            "ok": True,
            "cache_state": "cached",
            "detail": "model metadata is cached",
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "verify", "01MODEL", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("verify_model", {"model_ref": "01MODEL"}, "blackbird")]
    assert result.output == "OK\t01MODEL\tcached\tmodel metadata is cached\n"


def test_cli_model_verify_deep_passes_deep_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry_id": "01MODEL",
            "ok": True,
            "cache_state": "cached",
            "detail": "model deep verified",
            "deep": True,
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "verify", "01MODEL", "--deep", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("verify_model", {"model_ref": "01MODEL", "deep": "true"}, "blackbird")
    ]
    assert result.output == "OK\t01MODEL\tcached\tmodel deep verified\n"


def test_cli_model_verify_echoes_baseline_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        return {
            "entry_id": "01MODEL",
            "ok": True,
            "cache_state": "cached",
            "detail": "baseline established — rerun to compare",
            "deep": True,
            "baseline_established": True,
            "warnings": [
                {
                    "kind": "baseline-established",
                    "detail": "baseline established — rerun to compare",
                }
            ],
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app, ["model", "verify", "01MODEL", "--deep"]
    )

    assert result.exit_code == 0, result.output
    # The caveat rides both a WARN line and the verdict detail column.
    assert "WARNING: baseline established — rerun to compare" in result.output
    assert (
        "OK\t01MODEL\tcached\tbaseline established — rerun to compare"
        in result.output
    )


def test_cli_model_remove_requires_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    monkeypatch.setattr(
        cli_module,
        "_agent_call",
        lambda method, params=None, *, target_name="local": calls.append(
            (method, params, target_name)
        ),
    )

    result = CliRunner().invoke(cli_module.app, ["model", "remove", "01MODEL"])

    assert result.exit_code == 2
    assert calls == []
    assert "use --yes to remove model metadata" in result.output


def test_cli_model_remove_passes_configs_dir_to_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry_id": "01MODEL",
            "source": "hf_repo",
            "removed_weights": False,
            "entry": {"display_name": "llama-pin"},
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "remove",
            "llama-pin",
            "--target",
            "blackbird",
            "--configs-dir",
            str(tmp_path),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "remove_model",
            {"model_ref": "llama-pin", "configs_dir": str(tmp_path)},
            "blackbird",
        )
    ]
    assert result.output == "removed model\t01MODEL\tllama-pin\n"


def test_cli_model_remove_force_passes_force_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, str]] = []

    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        calls.append((method, params, target_name))
        return {
            "entry_id": "01MODEL",
            "source": "hf_repo",
            "removed_weights": False,
            "entry": {"display_name": "llama-pin"},
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "remove", "llama-pin", "--yes", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "remove_model",
            {"model_ref": "llama-pin", "force": "true"},
            "local",
        )
    ]
    assert result.output == "removed model\t01MODEL\tllama-pin\n"


def test_cli_model_remove_reports_expected_freed_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        assert method == "remove_model"
        assert params == {"model_ref": "llama-pin"}
        assert target_name == "local"
        return {
            "entry_id": "01MODEL",
            "source": "hf_repo",
            "removed_weights": True,
            "expected_freed_size": 1_500_000_000,
            "entry": {"display_name": "llama-pin"},
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "remove", "llama-pin", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "removed model\t01MODEL\tllama-pin\tfreed ~1.5 GB\n"


def test_cli_model_remove_reports_unique_and_nominal_freed_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_agent_call(
        method: str,
        params: dict[str, str] | None = None,
        *,
        target_name: str = "local",
    ):
        assert method == "remove_model"
        assert params == {"model_ref": "llama-pin"}
        assert target_name == "local"
        return {
            "entry_id": "01MODEL",
            "source": "hf_repo",
            "removed_weights": True,
            "expected_freed_size": 2_100_000_000,
            "entry": {
                "display_name": "llama-pin",
                "unique_size_bytes": 2_100_000_000,
                "nominal_size_bytes": 16_060_530_000,
            },
        }

    monkeypatch.setattr(cli_module, "_agent_call", fake_agent_call)

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "remove", "llama-pin", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert (
        result.output
        == "removed model\t01MODEL\tllama-pin\tfreed ~2.1 GB unique / 16.1 GB nominal\n"
    )


def test_cli_model_download_streams_job_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeUuid:
        hex = "job-model-1"

    class FakeEvents:
        def __init__(self) -> None:
            self._events = iter(
                [
                    {
                        "event": "job_progress",
                        "job_id": "job-model-1",
                        "kind": "committed",
                        "text": "Resolving model",
                        "level": "INFO",
                    },
                    {
                        "event": "job_done",
                        "job_id": "job-model-1",
                        "ok": True,
                        "detail": "model cached",
                    },
                ]
            )
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self) -> None:
            self.closed = True

    class FakeTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, str]]] = []
            self.subscribe_calls: list[tuple[list[str], object]] = []
            self.events = FakeEvents()

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "download_model":
                return {
                    "job_id": params["job_id"],
                    "kind": "download_model",
                    "status": "running",
                }
            raise AssertionError(f"unexpected target client call: {method}")

        def subscribe(self, run_ids, *, resume_from="live"):
            self.subscribe_calls.append((run_ids, resume_from))
            return self.events

    target_client = FakeTargetClient()
    monkeypatch.setattr(cli_module.uuid, "uuid4", lambda: FakeUuid())
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target_name: target_client,
    )

    result = CliRunner().invoke(
        cli_module.app,
        ["model", "download", "01MODEL", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    assert target_client.subscribe_calls == [(["job-model-1"], "live")]
    assert target_client.calls == [
        ("download_model", {"job_id": "job-model-1", "model_ref": "01MODEL"})
    ]
    assert target_client.events.closed is True
    assert result.output.splitlines() == [
        "Resolving model",
        "DONE\tjob-model-1\tmodel cached",
    ]


def test_cli_model_download_json_outputs_final_job_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeUuid:
        hex = "job-model-json"

    class FakeEvents:
        def __init__(self) -> None:
            self.closed = False
            self._events = iter(
                [
                    {
                        "event": "job_progress",
                        "job_id": "job-model-json",
                        "kind": "committed",
                        "text": "Resolving model",
                        "level": "INFO",
                    },
                    {
                        "event": "job_done",
                        "job_id": "job-model-json",
                        "ok": True,
                        "detail": "model cached",
                        "entry_id": "01MODEL",
                    },
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self) -> None:
            self.closed = True

    class FakeTargetClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.events = FakeEvents()

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            self.calls.append((method, params))
            return {
                "job_id": params["job_id"],
                "kind": "download_model",
                "status": "running",
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["job-model-json"]
            assert resume_from == "live"
            return self.events

    target_client = FakeTargetClient()
    monkeypatch.setattr(cli_module.uuid, "uuid4", lambda: FakeUuid())
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target_name: target_client,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "download",
            "01MODEL",
            "--target",
            "blackbird",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert target_client.calls == [
        ("download_model", {"job_id": "job-model-json", "model_ref": "01MODEL"})
    ]
    assert json.loads(result.output) == {
        "detail": "model cached",
        "entry_id": "01MODEL",
        "event": "job_done",
        "job_id": "job-model-json",
        "ok": True,
    }
    assert target_client.events.closed is True


def test_cli_model_download_passes_allow_and_ignore_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeUuid:
        hex = "job-model-patterns"

    class FakeEvents:
        def __init__(self) -> None:
            self.closed = False
            self._events = iter(
                [
                    {
                        "event": "job_done",
                        "job_id": "job-model-patterns",
                        "ok": True,
                        "detail": "model cached",
                    }
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self) -> None:
            self.closed = True

    class FakeTargetClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.events = FakeEvents()

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            self.calls.append((method, params))
            return {
                "job_id": params["job_id"],
                "kind": "download_model",
                "status": "running",
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            return self.events

    target_client = FakeTargetClient()
    monkeypatch.setattr(cli_module.uuid, "uuid4", lambda: FakeUuid())
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target_name: target_client,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "model",
            "download",
            "01MODEL",
            "--allow",
            "*.safetensors",
            "--allow",
            "*.json",
            "--ignore",
            "*.msgpack",
            "--target",
            "blackbird",
        ],
    )

    assert result.exit_code == 0, result.output
    assert target_client.calls == [
        (
            "download_model",
            {
                "job_id": "job-model-patterns",
                "model_ref": "01MODEL",
                "allow_patterns": ["*.safetensors", "*.json"],
                "ignore_patterns": ["*.msgpack"],
            },
        )
    ]


def test_cli_targets_list_prints_registry_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        agent_command=["vela", "agent", "connect"],
    )

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [TargetConfig(name="local"), blackbird]

    monkeypatch.setattr(
        cli_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )

    result = CliRunner().invoke(cli_module.app, ["targets", "list"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "local\tlocal\t-",
        "blackbird\tssh\tbgconley@10.25.0.51",
    ]


def test_cli_targets_test_handshakes_with_selected_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        agent_command=["vela", "agent", "connect"],
    )
    requested_target_names: list[str] = []
    requested_targets: list[TargetConfig] = []
    client_events: list[str] = []

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [TargetConfig(name="local"), blackbird]

        def by_name(self, name: str) -> TargetConfig:
            requested_target_names.append(name)
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    class FakeTargetClient:
        async def connect(self) -> None:
            client_events.append("connect")

        async def disconnect(self) -> None:
            client_events.append("disconnect")

        async def call(self, method: str, params):
            client_events.append(f"call:{method}")
            assert params is None
            if method == "handshake":
                return {
                    "target": "blackbird",
                    "agent_version": "1.2.3",
                    "protocol_version": 7,
                }
            if method == "diagnose":
                return {
                    "host": {
                        "hostname": "fake-blackbird",
                        "platform": "Linux",
                        "driver": "590.48.01",
                        "vela_version": "1.2.3",
                    },
                    "paths": {
                        "config_dir": "/home/bgconley/.config/vela",
                        "runs_dir": "/home/bgconley/.local/state/vela/runs",
                        "builds_dir": "/home/bgconley/.local/share/vela/builds",
                        "models_registry": "/home/bgconley/.local/share/vela/models.json",
                        "socket_path": "/home/bgconley/.local/state/vela/agent.sock",
                    },
                    "toolchain": {
                        "python": "/usr/bin/python3",
                        "uv_available": True,
                    },
                    "auth": {"status": "none"},
                }
            raise AssertionError(f"unexpected target client call: {method}")

    def fake_target_client_for_config(target):
        requested_targets.append(target)
        return FakeTargetClient()

    monkeypatch.setattr(
        cli_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(cli_module, "target_client_for_config", fake_target_client_for_config)

    result = CliRunner().invoke(cli_module.app, ["targets", "test", "blackbird"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "blackbird\tok\tagent=1.2.3\tprotocol=7"
    assert "version\tmismatch\tagent=1.2.3" in result.output
    assert "host\thostname=fake-blackbird platform=Linux" in result.output
    assert "paths\tconfig=/home/bgconley/.config/vela" in result.output
    assert "toolchain\tpython=/usr/bin/python3 uv=yes driver=590.48.01" in result.output
    assert "auth\tnone" in result.output
    assert requested_target_names == ["blackbird"]
    assert requested_targets == [blackbird, blackbird]
    assert client_events == [
        "connect",
        "call:handshake",
        "disconnect",
        "connect",
        "call:diagnose",
        "disconnect",
    ]


def test_agent_start_text_failure_names_stderr_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Phase-6 follow-up: on a start-failure the TEXT path must name the captured stderr
    # log (agent-start.err) the same way --json / the remediation surface already do —
    # otherwise the operator has no pointer to why the daemon died.
    from vela.agent import daemon as daemon_module

    def _fake_start(socket_path: Path | None = None, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "start-failed",
            "socket_path": "/tmp/vela/agent.sock",
            "stderr_log": "/tmp/vela/agent-start.err",
        }

    monkeypatch.setattr(daemon_module, "start_agent_daemon_process", _fake_start)
    result = CliRunner().invoke(cli_module.app, ["agent", "start"])
    assert result.exit_code == 1
    assert "agent-start.err" in result.output


def test_cli_targets_test_handshake_error_uses_target_name_in_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        agent_command=["vela", "agent", "connect"],
    )

    class FakeTargetsRegistry:
        @property
        def targets(self) -> list[TargetConfig]:
            return [TargetConfig(name="local"), blackbird]

        def by_name(self, name: str) -> TargetConfig:
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    class FakeTargetClient:
        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def call(self, method: str, params):
            assert method == "handshake"
            assert params is None
            raise cli_module.TargetCallError(
                "agent-unreachable",
                "SSH authentication failed",
                {"reason": "ssh-auth", "stderr": "Permission denied"},
            )

    monkeypatch.setattr(
        cli_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target: FakeTargetClient(),
    )

    result = CliRunner().invoke(cli_module.app, ["targets", "test", "blackbird"])

    assert result.exit_code == 2
    assert "ERROR AGENT_UNREACHABLE: SSH authentication failed" in result.output
    assert "SSH stderr: Permission denied" in result.output
    assert "vela targets setup-ssh blackbird" in result.output
    assert "TargetConfig(" not in result.output


def test_cli_targets_test_surfaces_invalid_ssh_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        agent_command=["vela", "agent", "connect"],
    )

    class FakeTargetsRegistry:
        @property
        def targets(self):
            return [TargetConfig(name="local"), blackbird]

        def by_name(self, name: str):
            if name == "blackbird":
                return blackbird
            raise KeyError(name)

    def fake_target_client_for_config(_target):
        raise ValueError("VELA_SSH_OPTS contains positional SSH argument 'evil'")

    monkeypatch.setattr(
        cli_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(cli_module, "target_client_for_config", fake_target_client_for_config)

    result = CliRunner().invoke(cli_module.app, ["targets", "test", "blackbird"])

    assert result.exit_code == 2
    assert (
        "ERROR: Unable to create target client: "
        "VELA_SSH_OPTS contains positional SSH argument 'evil'"
    ) in result.output


def test_cli_targets_setup_ssh_invokes_ssh_copy_id_with_target_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "ssh-copy-id.json"
    fake_copy_id = bin_dir / "ssh-copy-id"
    fake_copy_id.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, os, sys",
                f"open({str(command_log)!r}, 'w', encoding='utf-8').write(",
                "    json.dumps(sys.argv[1:])",
                ")",
                "print('keys installed')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_copy_id.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    key_path = tmp_path / "vela_ed25519.pub"
    targets_path = tmp_path / "vela" / "targets.yaml"
    targets_path.parent.mkdir()
    write_yaml(
        targets_path,
        f"""
        targets:
          blackbird:
            transport: ssh
            host: bgconley@fake
            ssh_key: {key_path}
        """,
    )

    result = CliRunner().invoke(cli_module.app, ["targets", "setup-ssh", "blackbird"])

    assert result.exit_code == 0, result.output
    assert "keys installed" in result.output
    assert "setup ssh\tblackbird\tbgconley@fake" in result.output
    assert json.loads(command_log.read_text(encoding="utf-8")) == [
        "-i",
        str(key_path),
        "bgconley@fake",
    ]


def test_cli_targets_add_persists_ssh_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = CliRunner().invoke(
        cli_module.app,
        [
            "targets",
            "add",
            "blackbird",
            "--transport",
            "ssh",
            "--host",
            "bgconley@10.25.0.51",
            "--ssh-key",
            "/home/bgconley/.ssh/vela_ed25519",
            "--agent-command",
            "/home/bgconley/venvs/current-vela/bin/vela agent connect",
            "--workdir",
            "/tank/repos/vela",
            "--venv",
            "/tank/venvs/vela",
            "--ssh-opts-env",
            "VELA_SSH_OPTS",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "added target blackbird\n"

    registry = load_targets_file(tmp_path / "vela" / "targets.yaml")
    blackbird = registry.by_name("blackbird")
    assert [target.name for target in registry.targets] == ["local", "blackbird"]
    assert blackbird.transport is TransportKind.SSH
    assert blackbird.host == "bgconley@10.25.0.51"
    assert blackbird.ssh_key == Path("/home/bgconley/.ssh/vela_ed25519")
    assert blackbird.agent_command == [
        "/home/bgconley/venvs/current-vela/bin/vela",
        "agent",
        "connect",
    ]
    assert blackbird.workdir == Path("/tank/repos/vela")
    assert blackbird.venv == Path("/tank/venvs/vela")
    assert blackbird.ssh_opts_env == "VELA_SSH_OPTS"


def test_cli_doctor_omits_static_next_steps_when_healthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VELA_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("VELA_AGENT_TOKEN_FILE", raising=False)

    result = CliRunner().invoke(cli_module.app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert any(check["name"] == "targets" for check in payload["checks"])
    assert any(check["name"] == "agent_token" for check in payload["checks"])
    assert payload["next_steps"] == []


def test_cli_targets_bootstrap_persists_target_and_agent_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    requested_targets: list[TargetConfig] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            assert method == "handshake"
            assert params is None
            return {"agent_version": "1.2.3", "protocol_version": 7}

    def fake_target_client_for_config(target: TargetConfig) -> FakeTargetClient:
        requested_targets.append(target)
        return FakeTargetClient()

    monkeypatch.setattr(cli_module, "target_client_for_config", fake_target_client_for_config)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "targets",
            "bootstrap",
            "blackbird",
            "--host",
            "bgconley@10.25.0.51",
            "--ssh-key",
            "/home/bgconley/.ssh/vela_ed25519",
            "--workdir",
            "/tank/repos/vela",
            "--agent-command",
            "/home/bgconley/venvs/current-vela/bin/vela agent connect",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "bootstrapped target blackbird" in result.output
    assert "OK\tagent\tprovided" in result.output
    assert "OK\thandshake\tagent=1.2.3\tprotocol=7" in result.output
    target = load_targets_file(tmp_path / "vela" / "targets.yaml").by_name("blackbird")
    assert target.ssh_key == Path("/home/bgconley/.ssh/vela_ed25519")
    assert target.agent_command == [
        "/home/bgconley/venvs/current-vela/bin/vela",
        "agent",
        "connect",
    ]
    assert requested_targets == [target]


def test_cli_targets_remove_deletes_named_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "vela").mkdir()
    targets_path = write_yaml(
        tmp_path / "vela" / "targets.yaml",
        """
        targets:
          blackbird:
            transport: ssh
            host: bgconley@10.25.0.51
        """,
    )

    result = CliRunner().invoke(cli_module.app, ["targets", "remove", "blackbird"])

    assert result.exit_code == 0, result.output
    assert result.output == "removed target blackbird\n"
    assert [target.name for target in load_targets_file(targets_path).targets] == ["local"]


def test_cli_deploy_create_preserves_blackbird_recipe_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blackbird_config = {
        "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
        "target": "blackbird",
        "model": "Qwen/Qwen3.6-27B-FP8",
        "served_model_name": "qwen36-27b-fp8-kvfp8-rp6000",
        "command": {
            "runtime": "docker",
            "entrypoint": "serve",
            "docker": {
                "image": (
                    "vllm/vllm-openai@sha256:"
                    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
                ),
                "container_name": "vela-qwen36-27b-fp8-kvfp8-rp6000-blackbird",
                "gpus": "all",
                "ipc_host": True,
                "shm_size": "32g",
                "network": "host",
                "hf_cache": "/home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache",
                "env": {
                    "FLASHINFER_CUDA_ARCH_LIST": "12.0f",
                    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                    "SAFETENSORS_FAST_GPU": "1",
                },
                "volumes": [
                    (
                        "/home/bgconley/models/qwen36-27b-fp8-rp6000/"
                        "flashinfer-cache:/root/.cache/flashinfer"
                    )
                ],
                "evict": ["qwen36-27b-bf16-rp6000-server"],
                "extra_run_args": ["--ulimit", "memlock=-1"],
            },
        },
        "engine": {
            "gpu_memory_utilization": 0.97,
            "max_model_len": 262144,
            "dtype": "auto",
            "kv_cache_dtype": "fp8",
            "max_num_seqs": 16,
        },
        "server": {"host": "0.0.0.0", "port": 18003, "exposure": "lan"},
        "extra_args": [
            "--attention-backend",
            "FLASHINFER",
            "--kv-cache-memory-bytes",
            "64424509440",
            "--language-model-only",
        ],
        "launch": {
            "mode": "attached",
            "ready_timeout_seconds": 1800,
            "runs_dir": "/home/bgconley/models/qwen36-27b-fp8-rp6000/vela-runs",
        },
        "vllm": {"version_profile": "0.11"},
    }
    calls: list[tuple[str, dict[str, object] | None]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "compose_config":
                assert params == {
                    "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
                    "target": "blackbird",
                    "runtime": {"kind": "docker"},
                    "model": "Qwen/Qwen3.6-27B-FP8",
                    "configs_dir": str(tmp_path),
                }
                return {
                    "config": blackbird_config,
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
                assert params == {"config": blackbird_config}
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                assert params == {"config": blackbird_config, "configs_dir": str(tmp_path)}
                return {
                    "preview": (
                        "docker run ... vllm/vllm-openai@sha256:b13d "
                        "--attention-backend FLASHINFER"
                    ),
                    "warnings": [],
                }
            if method == "preflight":
                assert params == {"config": blackbird_config, "configs_dir": str(tmp_path)}
                return {"ok": True, "checks": []}
            if method == "save_config":
                assert params == {
                    "configs_dir": str(tmp_path),
                    "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
                    "config": blackbird_config,
                }
                return {
                    "path": str(tmp_path / "qwen36-27b-fp8-kvfp8-rp6000-blackbird.yaml"),
                    "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
                    "config": blackbird_config,
                }
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "create",
            "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
            "--target",
            "blackbird",
            "--configs-dir",
            str(tmp_path),
            "--runtime",
            "docker",
            "--model",
            "Qwen/Qwen3.6-27B-FP8",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    docker = payload["config"]["command"]["docker"]
    assert payload["saved"]["path"].endswith("qwen36-27b-fp8-kvfp8-rp6000-blackbird.yaml")
    assert docker["image"].endswith(
        "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
    )
    assert docker["env"]["FLASHINFER_CUDA_ARCH_LIST"] == "12.0f"
    assert "/root/.cache/flashinfer" in docker["volumes"][0]
    assert "--attention-backend" in payload["config"]["extra_args"]
    assert "FLASHINFER" in payload["config"]["extra_args"]
    assert [method for method, _params in calls] == [
        "compose_config",
        "validate_config",
        "preview",
        "preflight",
        "save_config",
    ]


def test_cli_deploy_create_dry_run_does_not_preflight_or_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {
        "name": "dry-run",
        "model": "org/model",
        "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
    }
    calls: list[tuple[str, dict[str, object] | None]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "compose_config":
                return {"config": config, "warnings": [], "derived": []}
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/model", "warnings": []}
            if method in {"preflight", "save_config"}:
                raise AssertionError(f"dry-run should not call {method}")
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "create",
            "dry-run",
            "--model",
            "org/model",
            "--configs-dir",
            str(tmp_path),
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config"]["name"] == "dry-run"
    assert "saved" not in payload
    assert [method for method, _params in calls] == [
        "compose_config",
        "validate_config",
        "preview",
    ]


def test_cli_deploy_create_refuses_existing_without_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {
        "name": "repeatable",
        "model": "org/model",
        "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
    }
    save_params: list[dict[str, object]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            if method == "compose_config":
                return {"config": config, "warnings": [], "derived": []}
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/model", "warnings": []}
            if method == "preflight":
                return {"ok": True, "checks": []}
            if method == "save_config":
                save_params.append(dict(params))
                raise TargetCallError(
                    "config-exists",
                    "config already exists: repeatable",
                    {"name": "repeatable", "path": str(tmp_path / "repeatable.yaml")},
                )
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "create",
            "repeatable",
            "--model",
            "org/model",
            "--configs-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2, result.output
    assert save_params == [
        {
            "name": "repeatable",
            "config": config,
            "configs_dir": str(tmp_path),
        }
    ]
    assert "ERROR: Config already exists: repeatable" in result.output
    assert "Use --overwrite to update it." in result.output


def test_cli_deploy_create_overwrite_updates_existing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {
        "name": "repeatable",
        "model": "org/model",
        "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
    }
    save_params: list[dict[str, object]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            if method == "compose_config":
                return {"config": config, "warnings": [], "derived": []}
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/model", "warnings": []}
            if method == "preflight":
                return {"ok": True, "checks": []}
            if method == "save_config":
                save_params.append(dict(params))
                return {
                    "path": str(tmp_path / "repeatable.yaml"),
                    "name": "repeatable",
                    "config": config,
                }
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "create",
            "repeatable",
            "--model",
            "org/model",
            "--configs-dir",
            str(tmp_path),
            "--overwrite",
        ],
    )

    assert result.exit_code == 0, result.output
    assert save_params == [
        {
            "name": "repeatable",
            "config": config,
            "configs_dir": str(tmp_path),
            "overwrite": True,
        }
    ]
    assert f"updated deployment\trepeatable\t{tmp_path / 'repeatable.yaml'}" in result.output


def _deploy_create_client_with_preflight(
    config: dict[str, object],
    preflight: dict[str, object],
    calls: list[str],
    *,
    saved_path: str,
):
    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append(method)
            if method == "compose_config":
                return {"config": config, "warnings": [], "derived": []}
            if method == "validate_config":
                return {"ok": True, "errors": [], "warnings": []}
            if method == "preview":
                return {"preview": "cwd=/agent\nvllm serve org/model", "warnings": []}
            if method == "preflight":
                return preflight
            if method == "save_config":
                return {"path": saved_path, "name": config["name"]}
            raise AssertionError(f"unexpected target call: {method}")

    return FakeTargetClient()


def test_cli_deploy_create_failed_preflight_blocks_save_and_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {
        "name": "gated",
        "model": "org/model",
        "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
    }
    calls: list[str] = []
    preflight = {
        "ok": False,
        "failures": [
            {"kind": "PORT_IN_USE", "detail": "port 18001 is already in use"}
        ],
        "warnings": [],
    }
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: _deploy_create_client_with_preflight(
            config, preflight, calls, saved_path=str(tmp_path / "gated.yaml")
        ),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "create",
            "gated",
            "--model",
            "org/model",
            "--configs-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "preflight: PORT_IN_USE — port 18001 is already in use" in result.output
    assert "save_config" not in calls


def test_cli_deploy_create_force_saves_past_failed_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {
        "name": "gated",
        "model": "org/model",
        "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
    }
    calls: list[str] = []
    preflight = {
        "ok": False,
        "failures": [
            {"kind": "PORT_IN_USE", "detail": "port 18001 is already in use"}
        ],
        "warnings": [],
    }
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: _deploy_create_client_with_preflight(
            config, preflight, calls, saved_path=str(tmp_path / "gated.yaml")
        ),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "create",
            "gated",
            "--model",
            "org/model",
            "--configs-dir",
            str(tmp_path),
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "save_config" in calls
    assert "preflight: PORT_IN_USE — port 18001 is already in use" in result.output
    assert "saved deployment\tgated" in result.output


def test_cli_deploy_create_json_reports_preflight_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {
        "name": "gated",
        "model": "org/model",
        "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
    }
    calls: list[str] = []
    preflight = {
        "ok": False,
        "failures": [
            {"kind": "PORT_IN_USE", "detail": "port 18001 is already in use"}
        ],
        "warnings": [],
    }
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: _deploy_create_client_with_preflight(
            config, preflight, calls, saved_path=str(tmp_path / "gated.yaml")
        ),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "create",
            "gated",
            "--model",
            "org/model",
            "--configs-dir",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["preflight_ok"] is False
    assert payload["preflight"]["failures"][0]["kind"] == "PORT_IN_USE"
    # --json is unchanged apart from preflight_ok: it still saves and lets the
    # caller decide off preflight_ok.
    assert "save_config" in calls
    assert payload["saved"]["name"] == "gated"


def test_cli_deploy_create_preflight_warnings_print_but_do_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = {
        "name": "warned",
        "model": "org/model",
        "server": {"host": "127.0.0.1", "port": 18001, "exposure": "local"},
    }
    calls: list[str] = []
    preflight = {
        "ok": True,
        "failures": [],
        "warnings": [
            {
                "kind": "docker-no-hf-cache-mount",
                "detail": "container cannot see the target HF cache",
            }
        ],
    }
    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: _deploy_create_client_with_preflight(
            config, preflight, calls, saved_path=str(tmp_path / "warned.yaml")
        ),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "create",
            "warned",
            "--model",
            "org/model",
            "--configs-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "WARNING: container cannot see the target HF cache" in result.output
    assert "save_config" in calls
    assert "saved deployment\twarned" in result.output


def test_cli_deploy_export_prints_agent_generated_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "export_config":
                assert params == {
                    "name": "qwen36-export",
                    "configs_dir": str(tmp_path),
                }
                return {
                    "name": "qwen36-export",
                    "script": "#!/usr/bin/env bash\nexec docker run image model\n",
                    "warnings": [],
                }
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "export",
            "qwen36-export",
            "--configs-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "#!/usr/bin/env bash\nexec docker run image model\n"
    assert calls == [
        (
            "export_config",
            {"name": "qwen36-export", "configs_dir": str(tmp_path)},
        )
    ]


def test_cli_deploy_from_wrapper_calls_target_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "migrate_wrapper_config":
                assert params == {
                    "src_name": "legacy-fp8",
                    "new_name": "legacy-fp8-native",
                    "configs_dir": str(tmp_path),
                    "dry_run": True,
                }
                return {
                    "name": "legacy-fp8-native",
                    "path": str(tmp_path / "legacy-fp8-native.yaml"),
                    "config": {
                        "name": "legacy-fp8-native",
                        "model": "Qwen/Qwen3.6-27B-FP8",
                        "command": {
                            "runtime": "docker",
                            "docker": {
                                "env": {"FLASHINFER_CUDA_ARCH_LIST": "12.0f"},
                                "image": "vllm/vllm-openai@sha256:abc",
                            },
                        },
                        "extra_args": ["--attention-backend", "FLASHINFER"],
                    },
                    "derived": [],
                    "warnings": ["wrapper-migration-review-required"],
                    "written": False,
                }
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "from-wrapper",
            "legacy-fp8",
            "legacy-fp8-native",
            "--target",
            "blackbird",
            "--configs-dir",
            str(tmp_path),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == (
        "WARNING: wrapper-migration-review-required\n"
        f"dry-run wrapper migration\tlegacy-fp8-native\t{tmp_path / 'legacy-fp8-native.yaml'}\n"
    )
    assert calls == [
        (
            "migrate_wrapper_config",
            {
                "src_name": "legacy-fp8",
                "new_name": "legacy-fp8-native",
                "configs_dir": str(tmp_path),
                "dry_run": True,
            },
        )
    ]


def test_cli_deploy_list_clone_delete_call_target_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "list_configs":
                assert params == {"configs_dir": str(tmp_path)}
                return {
                    "valid": [
                        {
                            "name": "source",
                            "model": "Qwen/Qwen3.6-27B-FP8",
                            "path": str(tmp_path / "source.yaml"),
                        }
                    ],
                    "invalid": [],
                }
            if method == "clone_config":
                assert params == {
                    "configs_dir": str(tmp_path),
                    "src_name": "source",
                    "new_name": "copy",
                    "overrides": {
                        "server": {"port": 18005},
                        "extra_args": ["--attention-backend", "FLASHINFER"],
                    },
                }
                return {
                    "name": "copy",
                    "path": str(tmp_path / "copy.yaml"),
                    "config": {"name": "copy", "model": "Qwen/Qwen3.6-27B-FP8"},
                    "derived": [],
                }
            if method == "delete_config":
                assert params == {
                    "configs_dir": str(tmp_path),
                    "name": "copy",
                }
                return {"name": "copy", "path": str(tmp_path / "copy.yaml"), "deleted": True}
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    list_result = CliRunner().invoke(
        cli_module.app,
        ["deploy", "list", "--configs-dir", str(tmp_path)],
    )
    clone_result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "clone",
            "source",
            "copy",
            "--configs-dir",
            str(tmp_path),
            "--set",
            "server.port=18005",
            "--extra-arg=--attention-backend",
            "--extra-arg",
            "FLASHINFER",
        ],
    )
    delete_result = CliRunner().invoke(
        cli_module.app,
        ["deploy", "delete", "copy", "--configs-dir", str(tmp_path), "--yes"],
    )

    assert list_result.exit_code == 0, list_result.output
    assert "source\tQwen/Qwen3.6-27B-FP8" in list_result.output
    assert clone_result.exit_code == 0, clone_result.output
    assert clone_result.output == f"cloned deployment\tcopy\t{tmp_path / 'copy.yaml'}\n"
    assert delete_result.exit_code == 0, delete_result.output
    assert delete_result.output == f"deleted deployment\tcopy\t{tmp_path / 'copy.yaml'}\n"
    assert [method for method, _params in calls] == [
        "list_configs",
        "clone_config",
        "delete_config",
    ]


def test_cli_deploy_edit_calls_target_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "edit_config":
                assert params == {
                    "configs_dir": str(tmp_path),
                    "name": "qwen3",
                    "overrides": {
                        "engine": {"dtype": "bfloat16", "max_num_seqs": 4},
                        "server": {"port": 18009},
                        "extra_args": ["--max-num-batched-tokens", "4096"],
                    },
                }
                return {
                    "name": "qwen3",
                    "path": str(tmp_path / "qwen3.yaml"),
                    "config": {
                        "name": "qwen3",
                        "model": "Qwen/Qwen3-32B",
                        "engine": {"dtype": "bfloat16", "max_num_seqs": 4},
                        "server": {"port": 18009},
                    },
                    "warnings": ["preview-warning"],
                    "updated": True,
                }
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "deploy",
            "edit",
            "qwen3",
            "--configs-dir",
            str(tmp_path),
            "--set",
            "engine.dtype=bfloat16",
            "--set",
            "engine.max_num_seqs=4",
            "--set",
            "server.port=18009",
            "--extra-arg=--max-num-batched-tokens",
            "--extra-arg",
            "4096",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == str(tmp_path / "qwen3.yaml")
    assert payload["config"]["engine"]["dtype"] == "bfloat16"
    assert [method for method, _params in calls] == ["edit_config"]


def test_cli_config_push_pull_lint_call_target_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "pushed.yaml"
    source.write_text(
        "\n".join(
            [
                "name: pushed",
                "model: /models/pushed",
                "server:",
                "  port: 18008",
                "  api_key: sk-live",
                "env:",
                "  HF_TOKEN: hf_live",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    pulled = tmp_path / "pulled.yaml"
    calls: list[tuple[str, dict[str, object] | None]] = []

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "push_config":
                assert params == {
                    "configs_dir": str(tmp_path / "target-configs"),
                    "yaml": source.read_text(encoding="utf-8"),
                    "overwrite": True,
                }
                raise cli_module.TargetCallError(
                    "invalid-config",
                    "pushed config is invalid",
                    {
                        "name": "pushed",
                        "validation": {
                            "ok": False,
                            "errors": [
                                {
                                    "field": "server.api_key",
                                    "message": (
                                        "contains a literal secret; prefer target env injection"
                                    ),
                                },
                                {
                                    "field": "env.HF_TOKEN",
                                    "message": (
                                        "contains a literal secret; prefer target env injection"
                                    ),
                                },
                            ],
                            "warnings": [
                                (
                                    "model uses a host-local absolute path; "
                                    "prefer model_ref for portability"
                                )
                            ],
                        },
                    },
                )
            if method == "pull_config":
                assert params == {
                    "configs_dir": str(tmp_path / "target-configs"),
                    "name": "pushed",
                }
                return {
                    "name": "pushed",
                    "path": "/target/configs/pushed.yaml",
                    "config": {"name": "pushed", "model": "/models/pushed"},
                    "yaml": "name: pushed\nmodel: /models/pushed\n",
                    "warnings": [],
                }
            if method == "lint_config":
                assert params == {"yaml": source.read_text(encoding="utf-8")}
                return {
                    "ok": False,
                    "errors": [
                        {
                            "field": "server.api_key",
                            "message": "contains a literal secret; prefer target env injection",
                        },
                        {
                            "field": "env.HF_TOKEN",
                            "message": "contains a literal secret; prefer target env injection",
                        },
                    ],
                    "warnings": [
                        "model uses a host-local absolute path; prefer model_ref for portability",
                    ],
                }
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )

    push_result = CliRunner().invoke(
        cli_module.app,
        [
            "config",
            "push",
            str(source),
            "--target",
            "blackbird",
            "--configs-dir",
            str(tmp_path / "target-configs"),
            "--overwrite",
            "--json",
        ],
    )
    pull_result = CliRunner().invoke(
        cli_module.app,
        [
            "config",
            "pull",
            "pushed",
            "--target",
            "blackbird",
            "--configs-dir",
            str(tmp_path / "target-configs"),
            "--output",
            str(pulled),
        ],
    )
    lint_result = CliRunner().invoke(
        cli_module.app,
        ["config", "lint", str(source), "--target", "blackbird", "--json"],
    )

    assert push_result.exit_code == 2, push_result.output
    assert "ERROR: Invalid config: pushed" in push_result.output
    assert (
        "server.api_key: contains a literal secret; prefer target env injection"
        in push_result.output
    )
    assert (
        "env.HF_TOKEN: contains a literal secret; prefer target env injection"
        in push_result.output
    )
    assert pull_result.exit_code == 0, pull_result.output
    assert pull_result.output == f"pulled config\tpushed\t{pulled}\n"
    assert pulled.read_text(encoding="utf-8") == "name: pushed\nmodel: /models/pushed\n"
    assert lint_result.exit_code == 0, lint_result.output
    lint_payload = json.loads(lint_result.output)
    assert lint_payload["ok"] is False
    assert lint_payload["errors"] == [
        {
            "field": "server.api_key",
            "message": "contains a literal secret; prefer target env injection",
        },
        {
            "field": "env.HF_TOKEN",
            "message": "contains a literal secret; prefer target env injection",
        },
    ]
    assert [method for method, _params in calls] == [
        "push_config",
        "pull_config",
        "lint_config",
    ]


def test_cli_config_edit_round_trips_through_editor_lint_and_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []
    original_yaml = "name: pushed\nmodel: /models/pushed\nserver:\n  port: 18001\n"
    edited_yaml = "name: pushed\nmodel: /models/pushed\nserver:\n  port: 18009\n"

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "pull_config":
                assert params == {
                    "configs_dir": str(tmp_path / "target-configs"),
                    "name": "pushed",
                }
                return {
                    "name": "pushed",
                    "path": "/target/configs/pushed.yaml",
                    "config": {"name": "pushed", "model": "/models/pushed"},
                    "yaml": original_yaml,
                    "warnings": [],
                }
            if method == "lint_config":
                assert params == {"yaml": edited_yaml}
                return {"ok": True, "errors": [], "warnings": ["host-path warning"]}
            if method == "push_config":
                assert params == {
                    "configs_dir": str(tmp_path / "target-configs"),
                    "name": "pushed",
                    "yaml": edited_yaml,
                    "overwrite": True,
                }
                return {
                    "name": "pushed",
                    "path": "/target/configs/pushed.yaml",
                    "config": {"name": "pushed", "model": "/models/pushed"},
                    "warnings": ["host-path warning"],
                }
            raise AssertionError(f"unexpected target call: {method}")

    def fake_edit(text: str, *, extension: str) -> str:
        assert text == original_yaml
        assert extension == ".yaml"
        return edited_yaml

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )
    monkeypatch.setattr(cli_module.typer, "edit", fake_edit)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "config",
            "edit",
            "pushed",
            "--target",
            "blackbird",
            "--configs-dir",
            str(tmp_path / "target-configs"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "WARNING: host-path warning" in result.output
    assert result.output.endswith("edited config\tpushed\t/target/configs/pushed.yaml\n")
    assert [method for method, _params in calls] == [
        "pull_config",
        "lint_config",
        "push_config",
    ]


def test_cli_config_edit_refuses_to_push_lint_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []
    original_yaml = "name: pushed\nmodel: /models/pushed\n"
    edited_yaml = "name: pushed\nmodel: /models/pushed\nserver:\n  api_key: sk-live\n"

    class FakeTargetClient:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

        async def call(self, method: str, params):
            calls.append((method, params))
            if method == "pull_config":
                return {
                    "name": "pushed",
                    "path": "/target/configs/pushed.yaml",
                    "config": {"name": "pushed", "model": "/models/pushed"},
                    "yaml": original_yaml,
                    "warnings": [],
                }
            if method == "lint_config":
                assert params == {"yaml": edited_yaml}
                return {
                    "ok": False,
                    "errors": [
                        {
                            "field": "server.api_key",
                            "message": "contains a literal secret; prefer target env injection",
                        }
                    ],
                    "warnings": [],
                }
            raise AssertionError(f"unexpected target call: {method}")

    monkeypatch.setattr(
        cli_module,
        "_target_client_for_name_or_exit",
        lambda target: FakeTargetClient(),
    )
    monkeypatch.setattr(
        cli_module.typer,
        "edit",
        lambda text, *, extension: edited_yaml,
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "config",
            "edit",
            "pushed",
            "--target",
            "blackbird",
            "--configs-dir",
            str(tmp_path / "target-configs"),
        ],
    )

    assert result.exit_code == 2
    assert (
        "server.api_key: contains a literal secret; prefer target env injection"
        in result.output
    )
    assert [method for method, _params in calls] == ["pull_config", "lint_config"]


def test_cli_run_preview_uses_target_client_factory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client_instances: list[object] = []
    requested_targets: list[str] = []

    class FakeTargetClient:
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, str]]] = []
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "preview":
                return {"preview": "target-preview", "warnings": ["heads up"]}
            raise AssertionError(f"unexpected target client call: {method}")

    def fake_target_client_for_config(target, **_kwargs):
        requested_targets.append(target.name)
        return FakeTargetClient()

    monkeypatch.setattr(
        cli_module, "target_client_for_config", fake_target_client_for_config, raising=False
    )
    monkeypatch.setattr(
        cli_module,
        "LocalAgent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI constructed a LocalAgent")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "InProcessTargetClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI constructed an InProcessTargetClient")
        ),
        raising=False,
    )

    cli_module.run_config("agent-cfg", configs_dir=tmp_path, preview_only=True)

    stdout, stderr = capsys.readouterr()
    assert requested_targets == ["local"]
    assert client_instances[0].calls == [
        ("preview", {"name": "agent-cfg", "configs_dir": str(tmp_path)})
    ]
    assert client_instances[0].connected is False
    assert stdout == "target-preview\n"
    assert stderr == "WARNING: heads up\n"


def test_cli_preview_reports_unsupported_required_flags_without_traceback(
    config_dir: Path, tmp_path: Path
) -> None:
    script = tmp_path / "unused_child.py"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
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

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vela.cli",
            "preview",
            "unsupported-required-flag",
            "--configs-dir",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "required vLLM flags are unavailable" in proc.stderr
    assert "--definitely-missing-flag" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_run_preview_prints_command_warnings_for_nonlocal_bind(config_dir: Path) -> None:
    write_yaml(
        config_dir / "public-preview.yaml",
        """
        name: public-preview
        model: fake/model
        server:
          host: 0.0.0.0
          exposure: public
        """,
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vela.cli",
            "run",
            "public-preview",
            "--preview",
            "--configs-dir",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "--host 0.0.0.0" in proc.stdout
    assert "WARNING:" in proc.stderr
    assert "reachable beyond localhost" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_run_reports_missing_executable_without_traceback(
    config_dir: Path, tmp_path: Path, unused_tcp_port: int
) -> None:
    missing_executable = tmp_path / "missing-vllm"
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

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vela.cli",
            "run",
            "missing-bin",
            "--configs-dir",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "Command not found" in proc.stderr
    assert "install vLLM" in proc.stderr
    assert "command.entrypoint: module" in proc.stderr
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("command", ["run", "smoke"])
def test_cli_launch_preflight_reports_missing_local_model_without_traceback(
    config_dir: Path, tmp_path: Path, command: str
) -> None:
    missing_model = tmp_path / "missing-model"
    marker = tmp_path / "should-not-launch"
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('launched')",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "missing-model.yaml",
        f"""
        name: missing-model
        model: {missing_model}
        command:
          entrypoint: serve
          executable: {child}
        """,
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vela.cli",
            command,
            "missing-model",
            "--configs-dir",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "ERROR MODEL_NOT_FOUND:" in proc.stderr
    assert str(missing_model) in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("command", ["run", "smoke"])
def test_cli_launch_preflight_reports_tensor_parallel_mismatch_without_traceback(
    config_dir: Path, tmp_path: Path, command: str
) -> None:
    marker = tmp_path / "should-not-launch"
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('launched')",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "tp-mismatch.yaml",
        f"""
        name: tp-mismatch
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        engine:
          tensor_parallel_size: 2
        env:
          CUDA_VISIBLE_DEVICES: "0"
        """,
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vela.cli",
            command,
            "tp-mismatch",
            "--configs-dir",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "ERROR TP_MISMATCH:" in proc.stderr
    assert "Configured world size 2" in proc.stderr
    assert "CUDA_VISIBLE_DEVICES=0" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("command", ["run", "smoke"])
def test_cli_launch_preflight_reports_occupied_port_without_traceback(
    config_dir: Path, tmp_path: Path, command: str
) -> None:
    marker = tmp_path / "should-not-launch"
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('launched')",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
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
              executable: {child}
            server:
              host: 127.0.0.1
              port: {port}
            """,
        )

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "vela.cli",
                command,
                "port-in-use",
                "--configs-dir",
                str(config_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    assert proc.returncode == 2
    assert "ERROR PORT_IN_USE:" in proc.stderr
    assert str(port) in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not marker.exists()


@pytest.mark.asyncio
async def test_cli_smoke_exits_after_ready_and_stops_attached_child(
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

    proc = await asyncio.create_subprocess_exec(
        "vela",
        "smoke",
        "fake",
        "--configs-dir",
        str(config_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

    assert proc.returncode == 0
    output = stdout.decode()
    assert f"READY http://127.0.0.1:{port}" in output
    assert "models=fake-model" in output
    assert stderr.decode() == ""
    await _wait_for_health(port, expected=False)


@pytest.mark.asyncio
async def test_cli_smoke_tui_runs_textual_load_and_stop_flow(config_dir: Path) -> None:
    port = _free_port()
    script = Path.cwd() / "scripts" / "fake_vllm_child.py"
    write_yaml(
        config_dir / "fake-tui.yaml",
        f"""
        name: fake-tui
        model: fake/model
        command:
          entrypoint: serve
          executable: {script}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          ready_timeout_seconds: 15
          health:
            interval_seconds: 0.05
        """,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "vela.cli",
            "smoke-tui",
            "fake-tui",
            "--configs-dir",
            str(config_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=20)
        stdout = stdout_b.decode()
        stderr = stderr_b.decode()

        assert proc.returncode == 0, stderr
        assert f"READY http://127.0.0.1:{port} models=fake-model" in stdout
        assert "run_id=" in stdout
        assert "VELA_SMOKE_RUN_ID\t" in stdout
        assert "Traceback" not in stderr
        await _wait_for_health(port, expected=False)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_wait_for_tui_stopped_waits_for_target_run_id(config_dir: Path) -> None:
    app = VelaApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"

        assert await cli_module._wait_for_tui_stopped(app, timeout=0.05) is False

        app.current_run_id = None
        app._set_phase(Phase.STOPPED)

        assert await cli_module._wait_for_tui_stopped(app, timeout=0.2) is True


def test_smoke_tui_stop_timeout_respects_docker_grace() -> None:
    process_cfg = cli_module.ModelConfig.model_validate(
        {"name": "process", "model": "fake/model"}
    )
    docker_cfg = cli_module.ModelConfig.model_validate(
        {
            "name": "docker",
            "model": "fake/model",
            "command": {
                "runtime": "docker",
                "docker": {
                    "image": "vllm/vllm-openai@sha256:abc",
                    "stop_grace_seconds": 90,
                },
            },
        }
    )

    assert cli_module._smoke_tui_stop_timeout(process_cfg) == 10
    assert cli_module._smoke_tui_stop_timeout(docker_cfg) == 100


def test_cli_smoke_tui_prepares_through_target_client(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "agent-tui.yaml",
        f"""
        name: agent-tui
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        server:
          host: 127.0.0.1
          port: 8125
        """,
    )
    client_instances: list[object] = []
    smoke_calls: list[tuple[str, Path | None, str]] = []
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

    class FakeAgent:
        def handle(self, method: str, _params=None):
            raise AssertionError(f"direct CLI handle call: {method}")

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
            if method == "prepare_launch":
                return {
                    "config": {
                        "name": "agent-tui",
                        "model": "fake/model",
                        "command": {
                            "entrypoint": "serve",
                            "executable": str(executable),
                        },
                        "server": {"host": "127.0.0.1", "port": 8125},
                    },
                    "build": {
                        "argv": [str(executable)],
                        "env": {},
                        "cwd": str(tmp_path),
                        "warnings": [],
                        "metadata": {},
                        "preview": "",
                    },
                    "preflight": None,
                }
            raise AssertionError(f"unexpected target client call: {method}")

    async def fake_smoke_tui(
        name: str, configs_dir: Path | None, target_name: str = "local"
    ) -> int:
        smoke_calls.append((name, configs_dir, target_name))
        return 0

    fake_agent = FakeAgent()
    monkeypatch.setattr(
        cli_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "LocalAgent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI constructed a LocalAgent")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(fake_agent),
    )
    monkeypatch.setattr(cli_module, "_smoke_tui_config_cli", fake_smoke_tui)
    monkeypatch.setattr(
        cli_module,
        "build_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct smoke-tui build")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "check_launch_preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct smoke-tui preflight")
        ),
        raising=False,
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli_module.smoke_tui_config(
            "agent-tui", configs_dir=config_dir, target="blackbird"
        )

    assert exc_info.value.exit_code == 0
    assert client_instances[0].calls == [
        ("prepare_launch", {"name": "agent-tui", "configs_dir": str(config_dir)})
    ]
    assert client_instances[0].connected is False
    assert smoke_calls == [("agent-tui", config_dir, "blackbird")]


def test_cli_smoke_tui_passes_build_model_revision_overrides(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "agent-tui-override.yaml",
        f"""
        name: agent-tui-override
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        """,
    )
    client_instances: list[object] = []
    smoke_calls: list[
        tuple[str, Path | None, str, str | None, str | None, str | None]
    ] = []
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
        def __init__(self) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, str]]] = []
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "prepare_launch":
                return {
                    "config": {
                        "name": "agent-tui-override",
                        "model": "fake/model",
                        "command": {
                            "entrypoint": "serve",
                            "executable": str(executable),
                        },
                    },
                    "build": {
                        "argv": [str(executable)],
                        "env": {},
                        "cwd": str(tmp_path),
                        "warnings": [],
                        "metadata": {},
                        "preview": "",
                    },
                    "preflight": None,
                }
            raise AssertionError(f"unexpected target client call: {method}")

    async def fake_smoke_tui(
        name: str,
        configs_dir: Path | None,
        target_name: str = "local",
        *,
        build_id: str | None = None,
        model_ref: str | None = None,
        revision: str | None = None,
    ) -> int:
        smoke_calls.append(
            (name, configs_dir, target_name, build_id, model_ref, revision)
        )
        return 0

    monkeypatch.setattr(
        cli_module,
        "load_targets_file",
        lambda: FakeTargetsRegistry(),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(),
    )
    monkeypatch.setattr(cli_module, "_smoke_tui_config_cli", fake_smoke_tui)

    with pytest.raises(typer.Exit) as exc_info:
        cli_module.smoke_tui_config(
            "agent-tui-override",
            configs_dir=config_dir,
            target="blackbird",
            build_id="01BUILD",
            model_ref="01MODEL",
            revision="abc123",
        )

    assert exc_info.value.exit_code == 0
    assert client_instances[0].calls == [
        (
            "prepare_launch",
            {
                "name": "agent-tui-override",
                "configs_dir": str(config_dir),
                "build_id": "01BUILD",
                "model_ref": "01MODEL",
                "revision": "abc123",
            },
        )
    ]
    assert smoke_calls == [
        ("agent-tui-override", config_dir, "blackbird", "01BUILD", "01MODEL", "abc123")
    ]


@pytest.mark.parametrize("command", ["preview", "run"])
def test_cli_reports_unknown_config_name_without_traceback(config_dir: Path, command: str) -> None:
    write_yaml(
        config_dir / "known.yaml",
        """
        name: known
        model: fake/model
        """,
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vela.cli",
            command,
            "missing",
            "--configs-dir",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "Unknown config: missing" in proc.stderr
    assert "Available configs: known" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "KeyError" not in proc.stderr


def test_cli_unknown_config_names_searched_dirs_and_daemon_cwd_hint(config_dir: Path) -> None:
    # bug-238: an unknown-config error names the dirs the agent searched + its cwd,
    # and hints that the local daemon keeps its first working directory. This is a
    # plan-mandated exception to the bug-225 wire-scrub (diagnostic surface).
    write_yaml(
        config_dir / "known.yaml",
        """
        name: known
        model: fake/model
        """,
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vela.cli",
            "run",
            "nope",
            "--configs-dir",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "Unknown config: nope" in proc.stderr
    assert "Searched (agent 'local', cwd " in proc.stderr
    assert str(config_dir) in proc.stderr
    assert "keeps its first working directory" in proc.stderr
    assert "vela agent restart" in proc.stderr
    assert "Available configs: known" in proc.stderr


@pytest.mark.parametrize("command", ["preview", "run"])
def test_cli_reports_invalid_named_config_without_traceback(config_dir: Path, command: str) -> None:
    write_yaml(
        config_dir / "bad.yaml",
        """
        name: bad
        server:
          port: not-a-port
        """,
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vela.cli",
            command,
            "bad",
            "--configs-dir",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "Invalid config: bad" in proc.stderr
    assert "bad.yaml" in proc.stderr
    assert "model: Field required" in proc.stderr
    assert "server.port" in proc.stderr
    assert "Unknown config" not in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_run_attached_launches_through_target_client(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "agent-attached.yaml",
        f"""
        name: agent-attached
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        """,
    )
    client_instances: list[object] = []

    class FakeAgent:
        def handle(self, method: str, params):
            assert method == "prepare_launch"
            return {
                "config": {
                    "name": "agent-attached",
                    "model": "fake/model",
                    "command": {"entrypoint": "serve", "executable": str(executable)},
                },
                "build": {
                    "argv": [str(executable)],
                    "env": {},
                    "cwd": str(tmp_path),
                    "warnings": [],
                    "metadata": {},
                    "preview": "",
                },
                "preflight": None,
            }

        def start_attached_run(self, *_args, **_kwargs):
            raise AssertionError("direct attached start")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.calls: list[tuple[str, dict[str, str]]] = []
            self.connected = False
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "prepare_launch":
                return self.agent.handle(method, params)
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "wait":
                return {"run_id": "run-1", "returncode": 0, "intentional": False}
            raise AssertionError(f"unexpected call: {method}")

        async def _events(self):
            yield {
                "event": "log",
                "run_id": "run-1",
                "kind": "committed",
                "text": "INFO agent log",
                "level": "INFO",
                "seq": 1,
                "ts": "2026-06-03T00:00:00Z",
                "mono": 1.0,
            }
            yield {
                "event": "exited",
                "run_id": "run-1",
                "returncode": 0,
                "intentional": False,
                "phase": "STOPPED",
                "seq": 2,
                "ts": "2026-06-03T00:00:01Z",
                "mono": 2.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-1"]
            assert resume_from == "live"
            return self._events()

    fake_agent = FakeAgent()
    monkeypatch.setattr(
        cli_module,
        "LocalAgent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI constructed a LocalAgent")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(fake_agent),
    )
    monkeypatch.setattr(
        process_manager_module,
        "start_attached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct attached start")
        ),
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli_module.run_config("agent-attached", configs_dir=config_dir)

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == 0
    assert len(client_instances) == 1
    calls = client_instances[0].calls
    assert calls[0] == (
        "prepare_launch",
        {"name": "agent-attached", "configs_dir": str(config_dir)},
    )
    assert calls[1][0] == "launch"
    assert calls[1][1]["name"] == "agent-attached"
    assert calls[1][1]["configs_dir"] == str(config_dir)
    assert isinstance(calls[1][1]["run_id"], str)
    assert calls[1][1]["run_id"]
    assert calls[2:] == [
        (
            "wait",
            {"run_id": "run-1"},
        ),
    ]
    assert client_instances[0].connected is False
    assert "INFO agent log" in captured.out


def test_cli_run_detached_launches_through_target_client(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "agent-detached.yaml",
        f"""
        name: agent-detached
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        launch:
          mode: detached
          runs_dir: {tmp_path / "runs"}
        """,
    )
    client_instances: list[object] = []

    class FakeAgent:
        def handle(self, method: str, params):
            assert method == "prepare_launch"
            return {
                "config": {
                    "name": "agent-detached",
                    "model": "fake/model",
                    "command": {"entrypoint": "serve", "executable": str(executable)},
                    "launch": {
                        "mode": "detached",
                        "runs_dir": str(tmp_path / "runs"),
                    },
                },
                "build": {
                    "argv": [str(executable)],
                    "env": {},
                    "cwd": str(tmp_path),
                    "warnings": [],
                    "metadata": {},
                    "preview": "",
                },
                "preflight": None,
            }

        def start_detached_run(self, *_args, **_kwargs):
            raise AssertionError("direct detached CLI start")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "prepare_launch":
                return self.agent.handle(method, params)
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "detached",
                    "status": "started",
                }
            raise AssertionError(f"unexpected target client call: {method}")

    fake_agent = FakeAgent()
    monkeypatch.setattr(
        cli_module,
        "LocalAgent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI constructed a LocalAgent")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(fake_agent),
    )
    monkeypatch.setattr(
        process_manager_module,
        "start_detached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct detached start")
        ),
    )

    cli_module.run_config("agent-detached", configs_dir=config_dir)

    captured = capsys.readouterr()
    assert "detached run started: run-1" in captured.out
    assert "sidecar:" not in captured.out
    assert "log:" not in captured.out
    launch_call = client_instances[0].calls[1]
    assert launch_call[0] == "launch"
    assert launch_call[1]["name"] == "agent-detached"
    assert launch_call[1]["configs_dir"] == str(config_dir)
    assert isinstance(launch_call[1]["run_id"], str)
    assert launch_call[1]["run_id"]


def test_cli_run_attached_passes_build_model_revision_overrides(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "agent-override.yaml",
        f"""
        name: agent-override
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        """,
    )
    client_instances: list[object] = []

    class FakeTargetClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []
            self.connected = False
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "prepare_launch":
                return {
                    "config": {
                        "name": "agent-override",
                        "model": "fake/model",
                        "command": {"entrypoint": "serve", "executable": str(executable)},
                    },
                    "build": {
                        "argv": [str(executable)],
                        "env": {},
                        "cwd": str(tmp_path),
                        "warnings": [],
                        "metadata": {},
                        "preview": "",
                    },
                    "preflight": None,
                }
            if method == "launch":
                return {
                    "run_id": "run-override",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "wait":
                return {
                    "run_id": "run-override",
                    "returncode": 0,
                    "intentional": False,
                }
            raise AssertionError(f"unexpected call: {method}")

        async def _events(self):
            yield {
                "event": "exited",
                "run_id": "run-override",
                "returncode": 0,
                "intentional": False,
                "phase": "STOPPED",
                "seq": 1,
                "ts": "2026-06-03T00:00:01Z",
                "mono": 1.0,
            }

        def subscribe(self, run_ids, *, resume_from="live"):
            assert run_ids == ["run-override"]
            assert resume_from == "live"
            return self._events()

    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(),
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli_module.run_config(
            "agent-override",
            configs_dir=config_dir,
            build_id="01BUILD",
            model_ref="01MODEL",
            revision="abc123",
        )

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == 0
    assert captured.err == ""
    assert len(client_instances) == 1
    assert client_instances[0].calls[0] == (
        "prepare_launch",
        {
            "name": "agent-override",
            "configs_dir": str(config_dir),
            "build_id": "01BUILD",
            "model_ref": "01MODEL",
            "revision": "abc123",
        },
    )
    launch_call = client_instances[0].calls[1]
    assert launch_call[0] == "launch"
    assert launch_call[1]["name"] == "agent-override"
    assert launch_call[1]["configs_dir"] == str(config_dir)
    assert launch_call[1]["build_id"] == "01BUILD"
    assert launch_call[1]["model_ref"] == "01MODEL"
    assert launch_call[1]["revision"] == "abc123"
    assert launch_call[1]["run_id"]


def test_cli_smoke_attached_uses_target_client(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "smoke-attached.yaml",
        f"""
        name: smoke-attached
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        server:
          host: 127.0.0.1
          port: 8123
        """,
    )

    client_instances: list[object] = []

    class FakeAgent:
        def handle(self, method: str, params):
            assert method == "prepare_launch"
            return {
                "config": {
                    "name": "smoke-attached",
                    "model": "fake/model",
                    "command": {"entrypoint": "serve", "executable": str(executable)},
                    "server": {"host": "127.0.0.1", "port": 8123},
                },
                "build": {
                    "argv": [str(executable)],
                    "env": {},
                    "cwd": str(tmp_path),
                    "warnings": [],
                    "metadata": {},
                    "preview": "",
                },
                "preflight": None,
            }

        def start_attached_run(self, *_args, **_kwargs):
            raise AssertionError("direct attached smoke start")

        async def probe_run_until_ready(self, *_args, **_kwargs):
            raise AssertionError("direct smoke probe")

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
            if method == "prepare_launch":
                return self.agent.handle(method, params)
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
                    "reachable_url": "http://10.25.0.51:18123",
                }
            if method == "stop":
                return {"run_id": "run-1", "signaled": True}
            if method == "wait":
                return {"run_id": "run-1", "returncode": 0, "intentional": True}
            raise AssertionError(f"unexpected call: {method}")

    fake_agent = FakeAgent()
    monkeypatch.setattr(
        cli_module,
        "LocalAgent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI constructed a LocalAgent")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(fake_agent),
    )
    monkeypatch.setattr(
        process_manager_module,
        "start_attached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct attached smoke start")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "probe_loop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct smoke probe")
        ),
        raising=False,
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli_module.smoke_config("smoke-attached", configs_dir=config_dir)

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == 0
    assert "READY http://10.25.0.51:18123 models=served" in captured.out
    assert len(client_instances) == 1
    assert client_instances[0].calls[0] == (
        "prepare_launch",
        {"name": "smoke-attached", "configs_dir": str(config_dir)},
    )
    launch_call = client_instances[0].calls[1]
    assert launch_call[0] == "launch"
    assert launch_call[1]["name"] == "smoke-attached"
    assert launch_call[1]["configs_dir"] == str(config_dir)
    assert isinstance(launch_call[1]["run_id"], str)
    assert launch_call[1]["run_id"]
    assert (
        "probe_until_ready",
        {"run_id": "run-1"},
    ) in client_instances[0].calls
    assert (
        "stop",
        {"run_id": "run-1", "interrupt_timeout": 2, "terminate_timeout": 2},
    ) in client_instances[0].calls
    assert (
        "wait",
        {"run_id": "run-1"},
    ) in client_instances[0].calls
    assert len(client_instances[0].calls) == 5
    assert client_instances[0].connected is False


def test_cli_smoke_attached_passes_build_model_revision_overrides(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "smoke-override.yaml",
        f"""
        name: smoke-override
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        server:
          host: 127.0.0.1
          port: 8123
        """,
    )
    client_instances: list[object] = []

    class FakeTargetClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []
            self.connected = False
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "prepare_launch":
                return {
                    "config": {
                        "name": "smoke-override",
                        "model": "fake/model",
                        "command": {"entrypoint": "serve", "executable": str(executable)},
                        "server": {"host": "127.0.0.1", "port": 8123},
                    },
                    "build": {
                        "argv": [str(executable)],
                        "env": {},
                        "cwd": str(tmp_path),
                        "warnings": [],
                        "metadata": {},
                        "preview": "",
                    },
                    "preflight": None,
                }
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
                    "detail": "ready",
                    "models": ["served"],
                    "error_kind": None,
                    "reachable_url": "http://10.25.0.51:18123",
                }
            if method == "stop":
                return {"run_id": "run-override", "signaled": True}
            if method == "wait":
                return {"run_id": "run-override", "returncode": 0, "intentional": True}
            raise AssertionError(f"unexpected call: {method}")

    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(),
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli_module.smoke_config(
            "smoke-override",
            configs_dir=config_dir,
            build_id="01BUILD",
            model_ref="01MODEL",
            revision="abc123",
        )

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == 0
    assert "READY http://10.25.0.51:18123 models=served" in captured.out
    assert len(client_instances) == 1
    assert client_instances[0].calls[0] == (
        "prepare_launch",
        {
            "name": "smoke-override",
            "configs_dir": str(config_dir),
            "build_id": "01BUILD",
            "model_ref": "01MODEL",
            "revision": "abc123",
        },
    )
    launch_call = client_instances[0].calls[1]
    assert launch_call[0] == "launch"
    assert launch_call[1]["name"] == "smoke-override"
    assert launch_call[1]["configs_dir"] == str(config_dir)
    assert launch_call[1]["build_id"] == "01BUILD"
    assert launch_call[1]["model_ref"] == "01MODEL"
    assert launch_call[1]["revision"] == "abc123"
    assert launch_call[1]["run_id"]


def test_cli_smoke_detached_uses_target_client(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "smoke-detached.yaml",
        f"""
        name: smoke-detached
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        server:
          host: 127.0.0.1
          port: 8124
        launch:
          mode: detached
          runs_dir: {tmp_path / "runs"}
        """,
    )
    client_instances: list[object] = []

    class FakeAgent:
        def handle(self, method: str, params):
            assert method == "prepare_launch"
            return {
                "config": {
                    "name": "smoke-detached",
                    "model": "fake/model",
                    "command": {"entrypoint": "serve", "executable": str(executable)},
                    "server": {"host": "127.0.0.1", "port": 8124},
                    "launch": {
                        "mode": "detached",
                        "runs_dir": str(tmp_path / "runs"),
                    },
                },
                "build": {
                    "argv": [str(executable)],
                    "env": {},
                    "cwd": str(tmp_path),
                    "warnings": [],
                    "metadata": {},
                    "preview": "",
                },
                "preflight": None,
            }

        def start_detached_run(self, *_args, **_kwargs):
            raise AssertionError("direct detached smoke start")

        async def probe_run_until_ready(self, run_id: str, *, emit) -> None:
            raise AssertionError("direct smoke probe")

        def stop_run(self, run_id: str, *, interrupt_timeout, terminate_timeout) -> None:
            raise AssertionError("direct detached smoke stop")

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []
            client_instances.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            self.calls.append((method, params))
            if method == "prepare_launch":
                return self.agent.handle(method, params)
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "detached",
                    "status": "started",
                }
            if method == "probe_until_ready":
                return {
                    "run_id": params["run_id"],
                    "ready": True,
                    "detail": "ready",
                    "models": ["served"],
                    "error_kind": None,
                    "reachable_url": "http://10.25.0.51:18124",
                }
            if method == "stop":
                return {"run_id": params["run_id"], "signaled": True}
            raise AssertionError(f"unexpected target client call: {method}")

    fake_agent = FakeAgent()
    monkeypatch.setattr(
        cli_module,
        "LocalAgent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI constructed a LocalAgent")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(fake_agent),
    )
    monkeypatch.setattr(
        process_manager_module,
        "start_detached",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct detached smoke start")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "probe_loop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct smoke probe")
        ),
        raising=False,
    )
    with pytest.raises(typer.Exit) as exc_info:
        cli_module.smoke_config("smoke-detached", configs_dir=config_dir)

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == 0
    assert "detached smoke run: run-1" in captured.out
    assert "READY http://10.25.0.51:18124 models=served" in captured.out
    launch_call = client_instances[0].calls[1]
    assert launch_call[0] == "launch"
    assert launch_call[1]["name"] == "smoke-detached"
    assert launch_call[1]["configs_dir"] == str(config_dir)
    assert isinstance(launch_call[1]["run_id"], str)
    assert launch_call[1]["run_id"]


def test_cli_smoke_does_not_fallback_to_controller_probe_url(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "smoke-no-url.yaml",
        f"""
        name: smoke-no-url
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        server:
          host: 0.0.0.0
          port: 8125
          exposure: lan
        """,
    )

    class FakeAgent:
        def handle(self, method: str, _params):
            assert method == "prepare_launch"
            return {
                "config": {
                    "name": "smoke-no-url",
                    "model": "fake/model",
                    "command": {"entrypoint": "serve", "executable": str(executable)},
                    "server": {"host": "0.0.0.0", "port": 8125, "exposure": "lan"},
                },
                "build": {
                    "argv": [str(executable)],
                    "env": {},
                    "cwd": str(tmp_path),
                    "warnings": [],
                    "metadata": {},
                    "preview": "",
                },
                "preflight": None,
            }

    class FakeTargetClient:
        def __init__(self, agent) -> None:
            self.agent = agent
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params):
            if method == "prepare_launch":
                return self.agent.handle(method, params)
            if method == "launch":
                return {
                    "run_id": "run-1",
                    "launch_mode": "attached",
                    "status": "started",
                }
            if method == "probe_until_ready":
                return {
                    "run_id": params["run_id"],
                    "ready": True,
                    "detail": "ready",
                    "models": ["served"],
                    "error_kind": None,
                }
            if method == "stop":
                return {"run_id": params["run_id"], "signaled": True}
            if method == "wait":
                return {"run_id": params["run_id"], "returncode": 0, "intentional": True}
            raise AssertionError(f"unexpected target client call: {method}")

    monkeypatch.setattr(
        cli_module,
        "target_client_for_config",
        lambda _target, **_kwargs: FakeTargetClient(FakeAgent()),
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli_module.smoke_config("smoke-no-url", configs_dir=config_dir)

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == 2
    assert "reachable_url" in captured.err
    assert "127.0.0.1:8125" not in captured.out


@pytest.mark.asyncio
async def test_cli_run_forwards_sigint_to_attached_child(config_dir: Path) -> None:
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
        """,
    )
    proc = subprocess.Popen(
        ["vela", "run", "fake", "--configs-dir", str(config_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        await _wait_for_health(port, expected=True)
        proc.send_signal(signal.SIGINT)
        await asyncio.to_thread(proc.wait, 5)
        await _wait_for_health(port, expected=False)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_cli_run_prints_committed_fake_child_logs(config_dir: Path) -> None:
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
        """,
    )
    proc = await asyncio.create_subprocess_exec(
        "vela",
        "run",
        "fake",
        "--configs-dir",
        str(config_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        assert b"Initializing a V1 LLM engine" in line
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_cli_run_detached_starts_supervisor_and_writes_scrubbed_artifacts(
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
          api_key: literal-api-key
        env:
          HF_TOKEN: hf_literal
        extra_args:
          - --ignored-secret
          - literal-api-key
          - --hf-token-copy
          - hf_literal
        launch:
          mode: detached
          runs_dir: {runs_dir}
        """,
    )

    proc = await asyncio.create_subprocess_exec(
        "vela",
        "run",
        "detached",
        "--configs-dir",
        str(config_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = (await asyncio.wait_for(proc.stdout.read(), timeout=5)).decode()
    await asyncio.wait_for(proc.wait(), timeout=5)

    try:
        assert proc.returncode == 0, output
        assert "detached run started" in output
        await _wait_for_health(port, expected=True)
        sidecars = list(runs_dir.glob("*.json"))
        sidecar_paths = [path for path in sidecars if not path.name.endswith(".manifest.json")]
        assert len(sidecar_paths) == 1
        sidecar = json.loads(sidecar_paths[0].read_text(encoding="utf-8"))
        manifest = json.loads(Path(sidecar["manifest_path"]).read_text(encoding="utf-8"))
        log_path = Path(manifest["active_log"]["path"])
        await _wait_for_log_text(log_path, "Uvicorn running")
        log_text = log_path.read_text(encoding="utf-8")

        assert sidecar["launch_mode"] == "detached"
        assert sidecar["schema_version"] == 1
        assert sidecar["pid"] > 0
        assert sidecar["supervisor_pid"] > 0
        assert sidecar["pgid"] == sidecar["pid"]
        assert sidecar["host"] == "127.0.0.1"
        assert sidecar["port"] == port
        assert "literal-api-key" not in json.dumps(sidecar)
        assert "hf_literal" not in json.dumps(sidecar)
        assert "literal-api-key" not in log_text
        assert "hf_literal" not in log_text
        assert "Uvicorn running" in log_text
        assert Path(manifest["active_log"]["path"]).stat().st_mode & 0o777 == 0o600
        assert verify_sidecar_from_system(sidecar_paths[0])
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_cli_run_detached_records_detected_vllm_version(
    config_dir: Path, tmp_path: Path
) -> None:
    port = _free_port()
    runs_dir = tmp_path / "runs"
    fake_child = Path.cwd() / "scripts" / "fake_vllm_child.py"
    versioned_vllm = tmp_path / "vllm-versioned"
    versioned_vllm.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os",
                "import sys",
                "",
                "if sys.argv[1:] == ['--version']:",
                "    print('vllm version 0.11.2')",
                "    raise SystemExit(0)",
                "if sys.argv[1:] in (['serve', '--help'], ['serve', '--help=all']):",
                "    print('usage: vllm serve [OPTIONS] MODEL')",
                "    print('  --served-model-name TEXT')",
                "    print('  --host TEXT')",
                "    print('  --port INTEGER')",
                "    print('  --disable-log-requests')",
                "    raise SystemExit(0)",
                "if sys.argv[1:2] == ['serve']:",
                (
                    "    os.execv(sys.executable, [sys.executable, "
                    f"{str(fake_child)!r}, *sys.argv[1:]])"
                ),
                "raise SystemExit(2)",
            ]
        ),
        encoding="utf-8",
    )
    versioned_vllm.chmod(0o755)
    write_yaml(
        config_dir / "detected-version.yaml",
        f"""
        name: detected-version
        model: fake/model
        command:
          entrypoint: serve
          executable: {versioned_vllm}
        server:
          host: 127.0.0.1
          port: {port}
        launch:
          mode: detached
          runs_dir: {runs_dir}
        """,
    )

    proc = await asyncio.create_subprocess_exec(
        "vela",
        "run",
        "detected-version",
        "--configs-dir",
        str(config_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = (await asyncio.wait_for(proc.stdout.read(), timeout=5)).decode()
    await asyncio.wait_for(proc.wait(), timeout=5)

    try:
        assert proc.returncode == 0, output
        sidecar_paths = [
            path for path in runs_dir.glob("*.json") if not path.name.endswith(".manifest.json")
        ]
        assert len(sidecar_paths) == 1
        sidecar = json.loads(sidecar_paths[0].read_text(encoding="utf-8"))
        assert sidecar["vllm_version"] == "0.11.2"
        assert sidecar["vllm_version_profile"] == "0.11"
    finally:
        await _cleanup_port(port)


def test_detached_supervisor_rotates_log_and_updates_manifest(tmp_path: Path) -> None:
    child_script = tmp_path / "emit_many_lines.py"
    child_script.write_text(
        "\n".join(
            [
                "for index in range(20):",
                "    print(f'INFO rotation line {index:02d}', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "run.log"
    manifest_path = tmp_path / "run.manifest.json"
    sidecar_path = tmp_path / "run.json"
    payload = {
        "argv": [sys.executable, str(child_script)],
        "env": {},
        "cwd": str(tmp_path),
        "manifest_path": str(manifest_path),
        "sidecar_path": str(sidecar_path),
        "run_id": "rotation-test",
        "config_name": "rotation-test",
        "config_snapshot": None,
        "vllm_version": None,
        "vllm_version_profile": None,
        "host": "127.0.0.1",
        "port": 8765,
        "served_model_names": [],
        "exposure": "local",
        "launch_mode": "detached",
        "log_rotate_bytes": 120,
    }

    returncode = run_supervisor(
        payload["argv"],
        {},
        str(tmp_path),
        log_path,
        secrets=[],
        payload=payload,
    )

    assert returncode == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["active_log"]["path"] != str(log_path)
    assert manifest["rotated"]
    active_log_path = Path(manifest["active_log"]["path"])
    rotated_paths = [Path(item["path"]) for item in manifest["rotated"]]
    assert active_log_path.exists()
    assert active_log_path.stat().st_mode & 0o777 == 0o600
    assert all(path.exists() for path in rotated_paths)
    combined_log = active_log_path.read_text(encoding="utf-8") + "".join(
        path.read_text(encoding="utf-8") for path in rotated_paths
    )
    assert "INFO rotation line 00" in combined_log
    assert "INFO rotation line 19" in combined_log


def test_detached_supervisor_runs_child_under_pty(tmp_path: Path) -> None:
    child_script = tmp_path / "emit_tty_state.py"
    child_script.write_text(
        "\n".join(
            [
                "import sys",
                "print(",
                "    f'INFO stdout_tty={sys.stdout.isatty()} stderr_tty={sys.stderr.isatty()}',",
                "    flush=True,",
                ")",
            ]
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "run.log"

    returncode = run_supervisor(
        [sys.executable, str(child_script)],
        {},
        str(tmp_path),
        log_path,
        secrets=[],
        payload=None,
    )

    assert returncode == 0
    assert "INFO stdout_tty=True stderr_tty=True" in log_path.read_text(
        encoding="utf-8"
    )


def test_detached_sidecar_scrubs_generic_secret_patterns_from_command_argv(
    tmp_path: Path,
) -> None:
    child_script = tmp_path / "sleep.py"
    child_script.write_text("import time\ntime.sleep(0.1)\n", encoding="utf-8")
    log_path = tmp_path / "run.log"
    manifest_path = tmp_path / "run.manifest.json"
    sidecar_path = tmp_path / "run.json"
    payload = {
        "argv": [
            sys.executable,
            str(child_script),
            "--api-key",
            "sk-sidecar-secret",
            "--header",
            "Authorization: Bearer sidecar-bearer",
            "--hf-token-copy",
            "hf_sidecar_secret",
        ],
        "env": {},
        "cwd": str(tmp_path),
        "manifest_path": str(manifest_path),
        "sidecar_path": str(sidecar_path),
        "run_id": "generic-secret-sidecar-test",
        "config_name": "generic-secret-sidecar-test",
        "config_snapshot": None,
        "vllm_version": None,
        "vllm_version_profile": None,
        "host": "127.0.0.1",
        "port": 8765,
        "served_model_names": [],
        "exposure": "local",
        "launch_mode": "detached",
    }

    returncode = run_supervisor(
        payload["argv"],
        {},
        str(tmp_path),
        log_path,
        secrets=[],
        payload=payload,
    )

    sidecar_text = sidecar_path.read_text(encoding="utf-8")
    sidecar = json.loads(sidecar_text)
    assert returncode == 0
    assert "sk-sidecar-secret" not in sidecar_text
    assert "sidecar-bearer" not in sidecar_text
    assert "hf_sidecar_secret" not in sidecar_text
    assert "Authorization: Bearer ••••" in sidecar["command_argv"]
    assert "••••" in sidecar["command_argv"]


def test_supervisor_keeps_manifest_active_log_consistent_when_rotation_manifest_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_script = tmp_path / "emit_after_rotation_manifest_failure.py"
    child_script.write_text(
        "\n".join(
            [
                "import sys",
                "import time",
                "print('INFO ' + 'x' * 6000, flush=True)",
                "time.sleep(0.05)",
                "print('INFO rotation-manifest-failure final line', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "run.log"
    manifest_path = tmp_path / "run.manifest.json"
    payload = {
        "argv": [sys.executable, str(child_script)],
        "env": {},
        "cwd": str(tmp_path),
        "manifest_path": str(manifest_path),
        "sidecar_path": str(tmp_path / "run.json"),
        "run_id": "rotation-manifest-fail-test",
        "config_name": "rotation-manifest-fail-test",
        "config_snapshot": None,
        "vllm_version": None,
        "vllm_version_profile": None,
        "host": "127.0.0.1",
        "port": 8765,
        "served_model_names": [],
        "exposure": "local",
        "launch_mode": "detached",
        "log_rotate_bytes": 120,
    }
    original_write_atomic = supervisor_module.Manifest.write_atomic
    write_count = 0

    def fail_rotation_manifest_write(self, path: Path) -> None:
        nonlocal write_count
        write_count += 1
        if write_count > 1:
            raise OSError("simulated rotation manifest failure")
        original_write_atomic(self, path)

    monkeypatch.setattr(
        supervisor_module.Manifest,
        "write_atomic",
        fail_rotation_manifest_write,
    )

    returncode = run_supervisor(
        payload["argv"],
        {},
        str(tmp_path),
        log_path,
        secrets=[],
        payload=payload,
    )

    assert returncode == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    active_log_text = Path(manifest["active_log"]["path"]).read_text(encoding="utf-8")
    assert "INFO rotation-manifest-failure final line" in active_log_text


def test_supervisor_drains_child_when_initial_log_open_fails(tmp_path: Path) -> None:
    child_script = tmp_path / "emit_output.py"
    child_script.write_text(
        "\n".join(
            [
                "for index in range(100):",
                "    print(f'INFO fallback-drain line {index:03d}', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("file blocks log directory creation", encoding="utf-8")

    returncode = run_supervisor(
        [sys.executable, str(child_script)],
        {},
        str(tmp_path),
        not_a_dir / "run.log",
        secrets=[],
    )

    assert returncode == 0


def test_supervisor_drains_child_when_artifact_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_script = tmp_path / "emit_after_artifact_failure.py"
    child_script.write_text(
        "\n".join(
            [
                "for index in range(100):",
                "    print(f'INFO artifact-fallback line {index:03d}', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "run.log"
    payload = {
        "argv": [sys.executable, str(child_script)],
        "env": {},
        "cwd": str(tmp_path),
        "manifest_path": str(tmp_path / "run.manifest.json"),
        "sidecar_path": str(tmp_path / "run.json"),
        "run_id": "artifact-fail-test",
        "config_name": "artifact-fail-test",
        "config_snapshot": None,
        "vllm_version": None,
        "vllm_version_profile": None,
        "host": "127.0.0.1",
        "port": 8765,
        "served_model_names": [],
        "exposure": "local",
        "launch_mode": "detached",
    }

    def fail_artifact_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated manifest write failure")

    monkeypatch.setattr(supervisor_module, "_write_run_artifacts", fail_artifact_write)

    returncode = run_supervisor(
        payload["argv"],
        {},
        str(tmp_path),
        log_path,
        secrets=[],
        payload=payload,
    )

    assert returncode == 0
    assert "INFO artifact-fallback line 099" in log_path.read_text(encoding="utf-8")


async def _wait_for_health(port: int, *, expected: bool) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    async with httpx.AsyncClient(timeout=0.2) as client:
        while asyncio.get_running_loop().time() < deadline:
            healthy = False
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health")
                healthy = response.status_code == 200
            except httpx.HTTPError:
                healthy = False
            if healthy is expected:
                return
            await asyncio.sleep(0.05)
    raise AssertionError(f"health expected={expected} was not observed on port {port}")


async def _wait_for_log_text(path: Path, text: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if path.exists() and text in path.read_text(encoding="utf-8"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{text!r} was not written to {path}")


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
                subprocess.run(["kill", "-TERM", pid_text.strip()], check=False)
            except Exception:
                pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize("argv", [["list"], ["build", "list"], ["model", "list"]])
def test_list_commands_surface_remediation_not_traceback_on_unreachable_target(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # First-run UX: an unreachable target must produce an actionable remediation
    # and a clean nonzero exit, never a raw Python traceback.
    def _raise(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise TargetCallError(
            "agent-unreachable",
            "ssh: connect to host gpu-node port 22: Connection refused",
            {},
        )

    monkeypatch.setattr(cli_module, "_agent_call", _raise)

    result = CliRunner().invoke(cli_module.app, [*argv, "--target", "gpu-node"])

    assert result.exit_code == 2, result.output
    assert not isinstance(result.exception, TargetCallError), result.output
