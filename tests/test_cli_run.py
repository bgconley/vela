from __future__ import annotations

import asyncio
import json
import os
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

from vllm_loader import __version__
from vllm_loader import cli as cli_module
from vllm_loader.cli import _enable_textual_debug_features
from vllm_loader.config.targets import TargetConfig, TransportKind, load_targets_file
from vllm_loader.engine import process_manager as process_manager_module
from vllm_loader.engine import supervisor as supervisor_module
from vllm_loader.engine.phases import Phase
from vllm_loader.engine.sidecar import verify_sidecar_from_system
from vllm_loader.engine.supervisor import run_supervisor
from vllm_loader.tui.app import VllmLoaderApp


def test_debug_mode_enables_textual_debug_and_devtools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXTUAL", "foo,debug")

    _enable_textual_debug_features()

    assert os.environ["TEXTUAL"] == "debug,devtools,foo"


def test_cli_root_version_option_prints_version_without_launching_tui() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "vllm_loader.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout.strip() == __version__
    assert proc.stderr == ""


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

    monkeypatch.setattr(cli_module, "VllmLoaderApp", FakeTui)

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


def test_cli_preview_target_option_uses_selected_target_from_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blackbird = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
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


def test_cli_targets_list_prints_registry_targets(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert result.output == "blackbird\tok\tagent=1.2.3\tprotocol=7\n"
    assert requested_target_names == ["blackbird"]
    assert requested_targets == [blackbird]
    assert client_events == ["connect", "call:handshake", "disconnect"]


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
            "--workdir",
            "/tank/repos/lab-tui",
            "--venv",
            "/tank/venvs/lab-tui",
            "--ssh-opts-env",
            "VLLM_LOADER_SSH_OPTS",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "added target blackbird\n"

    registry = load_targets_file(tmp_path / "vllm-loader" / "targets.yaml")
    blackbird = registry.by_name("blackbird")
    assert [target.name for target in registry.targets] == ["local", "blackbird"]
    assert blackbird.transport is TransportKind.SSH
    assert blackbird.host == "bgconley@10.25.0.51"
    assert blackbird.workdir == Path("/tank/repos/lab-tui")
    assert blackbird.venv == Path("/tank/venvs/lab-tui")
    assert blackbird.ssh_opts_env == "VLLM_LOADER_SSH_OPTS"


def test_cli_targets_remove_deletes_named_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "vllm-loader").mkdir()
    targets_path = write_yaml(
        tmp_path / "vllm-loader" / "targets.yaml",
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
            "vllm_loader.cli",
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
            "vllm_loader.cli",
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
    config_dir: Path, tmp_path: Path
) -> None:
    missing_executable = tmp_path / "missing-vllm"
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

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vllm_loader.cli",
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
            "vllm_loader.cli",
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
            "vllm_loader.cli",
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
                "vllm_loader.cli",
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
        "vllm-loader",
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
            "vllm_loader.cli",
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
        assert "Traceback" not in stderr
        await _wait_for_health(port, expected=False)
    finally:
        await _cleanup_port(port)


@pytest.mark.asyncio
async def test_wait_for_tui_stopped_waits_for_target_run_id(config_dir: Path) -> None:
    app = VllmLoaderApp(configs_dir=config_dir)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_run_id = "run-1"

        assert await cli_module._wait_for_tui_stopped(app, timeout=0.05) is False

        app.current_run_id = None
        app._set_phase(Phase.STOPPED)

        assert await cli_module._wait_for_tui_stopped(app, timeout=0.2) is True


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
    smoke_calls: list[tuple[str, Path | None]] = []

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

    async def fake_smoke_tui(name: str, configs_dir: Path | None) -> int:
        smoke_calls.append((name, configs_dir))
        return 0

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
        cli_module.smoke_tui_config("agent-tui", configs_dir=config_dir)

    assert exc_info.value.exit_code == 0
    assert client_instances[0].calls == [
        ("prepare_launch", {"name": "agent-tui", "configs_dir": str(config_dir)})
    ]
    assert client_instances[0].connected is False
    assert smoke_calls == [("agent-tui", config_dir)]


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
            "vllm_loader.cli",
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
            "vllm_loader.cli",
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
    assert "READY http://127.0.0.1:8123 models=served" in captured.out
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
    assert "READY http://127.0.0.1:8124 models=served" in captured.out
    launch_call = client_instances[0].calls[1]
    assert launch_call[0] == "launch"
    assert launch_call[1]["name"] == "smoke-detached"
    assert launch_call[1]["configs_dir"] == str(config_dir)
    assert isinstance(launch_call[1]["run_id"], str)
    assert launch_call[1]["run_id"]


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
        ["vllm-loader", "run", "fake", "--configs-dir", str(config_dir)],
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
        "vllm-loader",
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
        "vllm-loader",
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
        "vllm-loader",
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
