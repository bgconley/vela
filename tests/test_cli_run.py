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
from conftest import write_yaml

from vllm_loader import __version__
from vllm_loader.cli import _enable_textual_debug_features
from vllm_loader.engine import supervisor as supervisor_module
from vllm_loader.engine.sidecar import verify_sidecar_from_system
from vllm_loader.engine.supervisor import run_supervisor


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
