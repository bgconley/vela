from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import vela.engine.supervisor as supervisor_module
from tests.fakes.fake_docker import write_fake_docker_runtime
from vela.engine.supervisor import run_supervisor


def _docker_payload(
    tmp_path: Path, docker_binary: Path, *, docker: dict, env: dict
) -> dict:
    return {
        "runtime": "docker",
        "argv": [str(docker_binary), "run", "-d", "image"],
        "env": env,
        "cwd": str(tmp_path),
        "log_path": str(tmp_path / "run.log"),
        "manifest_path": str(tmp_path / "run.manifest.json"),
        "sidecar_path": str(tmp_path / "run.json"),
        "exit_status_path": str(tmp_path / "run.exit-status"),
        "event_log_path": str(tmp_path / "run.events.ndjson"),
        "secrets": [],
        "run_id": "run",
        "config_name": "cfg",
        "command_hash": "sha256:command",
        "host": "127.0.0.1",
        "port": 8000,
        "exposure": "local",
        "launch_mode": "detached",
        "docker": docker,
    }


def _write_failing_docker(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import sys",
                "args = sys.argv[1:]",
                "if args[:3] == ['image', 'inspect', 'image']:",
                "    payload = [{'Id': 'sha256:resolved',",
                "                'RepoDigests': ['image@sha256:resolved']}]",
                "    print(json.dumps(payload))",
                "    raise SystemExit(0)",
                "if args[:2] == ['run', '-d']:",
                "    print('docker cannot start sk-secret-container', file=sys.stderr)",
                "    raise SystemExit(125)",
                "raise SystemExit(0)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_logging_docker(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "import sys",
                "args = sys.argv[1:]",
                "if args[:3] == ['image', 'inspect', 'image']:",
                "    payload = [{'Id': 'sha256:resolved',",
                "                'RepoDigests': ['image@sha256:resolved']}]",
                "    print(json.dumps(payload))",
                "    raise SystemExit(0)",
                "if args[:2] == ['run', '-d']:",
                "    print('container-123')",
                "    raise SystemExit(0)",
                "if args[:2] == ['logs', '-f']:",
                "    fixture = os.environ.get('FAKE_DOCKER_LOG_FIXTURE')",
                "    if fixture:",
                "        with open(fixture, encoding='utf-8') as file:",
                "            print(file.read(), end='', flush=True)",
                "    raise SystemExit(0)",
                "if args[:1] == ['wait']:",
                "    print('0')",
                "    raise SystemExit(0)",
                "if args[:1] == ['rm']:",
                "    raise SystemExit(0)",
                "raise SystemExit(0)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_docker_supervisor_writes_scrubbed_run_stderr_to_log(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    _write_failing_docker(docker)
    log_path = tmp_path / "run.log"
    payload = {
        "runtime": "docker",
        "argv": [str(docker), "run", "-d", "image"],
        "env": {},
        "cwd": str(tmp_path),
        "log_path": str(log_path),
        "manifest_path": str(tmp_path / "run.manifest.json"),
        "sidecar_path": str(tmp_path / "run.json"),
        "exit_status_path": str(tmp_path / "run.exit-status"),
        "event_log_path": str(tmp_path / "run.events.ndjson"),
        "secrets": ["sk-secret-container"],
        "run_id": "run",
        "config_name": "cfg",
        "command_hash": "sha256:command",
        "host": "127.0.0.1",
        "port": 8000,
        "exposure": "local",
        "launch_mode": "detached",
        "docker": {"binary": str(docker), "image": "image"},
    }

    returncode = run_supervisor(
        payload["argv"],
        payload["env"],
        payload["cwd"],
        log_path,
        payload["secrets"],
        payload=payload,
    )

    assert returncode == 125
    log = log_path.read_text(encoding="utf-8")
    assert "docker run failed" in log
    assert "docker cannot start" in log
    assert "sk-secret-container" not in log
    exit_status = json.loads((tmp_path / "run.exit-status").read_text(encoding="utf-8"))
    assert exit_status["returncode"] == 125


def test_docker_supervisor_scrubs_container_logs_and_events(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    _write_logging_docker(docker)
    log_fixture = Path("tests/fixtures/docker_logs/ready-with-secret.log").resolve()
    log_path = tmp_path / "run.log"
    event_log_path = tmp_path / "run.events.ndjson"
    payload = {
        "runtime": "docker",
        "argv": [str(docker), "run", "-d", "image"],
        "env": {"FAKE_DOCKER_LOG_FIXTURE": str(log_fixture)},
        "cwd": str(tmp_path),
        "log_path": str(log_path),
        "manifest_path": str(tmp_path / "run.manifest.json"),
        "sidecar_path": str(tmp_path / "run.json"),
        "exit_status_path": str(tmp_path / "run.exit-status"),
        "event_log_path": str(event_log_path),
        "secrets": ["sk-secret-container"],
        "run_id": "run",
        "config_name": "cfg",
        "command_hash": "sha256:command",
        "host": "127.0.0.1",
        "port": 8000,
        "exposure": "local",
        "launch_mode": "detached",
        "docker": {"binary": str(docker), "image": "image"},
    }

    returncode = run_supervisor(
        payload["argv"],
        payload["env"],
        payload["cwd"],
        log_path,
        payload["secrets"],
        payload=payload,
    )

    assert returncode == 0
    log = log_path.read_text(encoding="utf-8")
    events = event_log_path.read_text(encoding="utf-8")
    assert "INFO loaded token" in log
    assert "sk-secret-container" not in log
    assert "sk-secret-container" not in events
    assert "••••" in log
    assert "Uvicorn running" in events


def test_docker_supervisor_stops_container_when_run_artifacts_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Orphan guard: if the sidecar/manifest cannot be persisted, the controller
    # can never track/stop the container — so the supervisor must stop+remove it
    # rather than leave an orphaned GPU container running untracked.
    docker = tmp_path / "docker"
    write_fake_docker_runtime(docker)
    cmd_log = tmp_path / "docker-cmd.log"
    log_path = tmp_path / "run.log"
    payload = {
        "runtime": "docker",
        "argv": [str(docker), "run", "-d", "image"],
        "env": {"FAKE_DOCKER_COMMAND_LOG": str(cmd_log)},
        "cwd": str(tmp_path),
        "log_path": str(log_path),
        "manifest_path": str(tmp_path / "run.manifest.json"),
        "sidecar_path": str(tmp_path / "run.json"),
        "exit_status_path": str(tmp_path / "run.exit-status"),
        "event_log_path": str(tmp_path / "run.events.ndjson"),
        "secrets": [],
        "run_id": "run",
        "config_name": "cfg",
        "command_hash": "sha256:command",
        "host": "127.0.0.1",
        "port": 8000,
        "exposure": "local",
        "launch_mode": "detached",
        "docker": {"binary": str(docker), "image": "image"},
    }

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(supervisor_module, "_write_docker_run_artifacts", _fail)

    returncode = run_supervisor(
        payload["argv"],
        payload["env"],
        payload["cwd"],
        log_path,
        payload["secrets"],
        payload=payload,
    )

    assert returncode == 1
    commands = cmd_log.read_text(encoding="utf-8")
    assert "stop container-123" in commands
    # Must not stream/wait on the untrackable container.
    assert "wait container-123" not in commands
    exit_status = json.loads((tmp_path / "run.exit-status").read_text(encoding="utf-8"))
    assert exit_status["returncode"] == 1


def test_docker_supervisor_classifies_pull_timeout_and_streams_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bug-240: a slow `docker pull` (pull: always/missing) on a ~10GB image must
    # time out into a classified failure — never crash the supervisor with an
    # uncaught TimeoutExpired — with pull progress visible in the run log, and,
    # because no container exists yet, must leave nothing to orphan.
    docker = tmp_path / "docker"
    write_fake_docker_runtime(docker)
    cmd_log = tmp_path / "docker-cmd.log"
    log_path = tmp_path / "run.log"
    monkeypatch.setenv("VELA_DOCKER_PULL_TIMEOUT_SECONDS", "0.4")
    payload = _docker_payload(
        tmp_path,
        docker,
        docker={"binary": str(docker), "image": "image", "pull": "always"},
        env={
            "FAKE_DOCKER_COMMAND_LOG": str(cmd_log),
            "FAKE_DOCKER_PULL_SLEEP_SECONDS": "30",
        },
    )

    returncode = run_supervisor(
        payload["argv"],
        payload["env"],
        payload["cwd"],
        log_path,
        payload["secrets"],
        payload=payload,
    )

    assert returncode == 124
    log = log_path.read_text(encoding="utf-8")
    assert "image-pull-timeout" in log
    assert "Pulling from library/image" in log
    exit_status = json.loads((tmp_path / "run.exit-status").read_text(encoding="utf-8"))
    assert exit_status["returncode"] == 124
    commands = cmd_log.read_text(encoding="utf-8")
    assert "pull image" in commands
    assert "run -d" not in commands


def test_docker_supervisor_streams_pull_progress_on_successful_pull(
    tmp_path: Path,
) -> None:
    # A successful pull must still stream its phase/progress lines through the
    # scrubbed sink so the TUI shows the download, then hand off to container
    # log streaming.
    docker = tmp_path / "docker"
    write_fake_docker_runtime(docker)
    log_path = tmp_path / "run.log"
    payload = _docker_payload(
        tmp_path,
        docker,
        docker={"binary": str(docker), "image": "image", "pull": "always"},
        env={},
    )

    returncode = run_supervisor(
        payload["argv"],
        payload["env"],
        payload["cwd"],
        log_path,
        payload["secrets"],
        payload=payload,
    )

    assert returncode == 0
    log = log_path.read_text(encoding="utf-8")
    assert "Pulling from library/image" in log
    assert "Uvicorn running" in log


def test_docker_supervisor_survives_a_prepare_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-pull docker prep command (e.g. `docker image inspect`) that blows its
    # short 10s timeout must not crash the supervisor: catch TimeoutExpired,
    # write a classified failure + exit-status, and never reach `docker run`
    # (no container exists to orphan).
    docker = tmp_path / "docker"
    write_fake_docker_runtime(docker)
    cmd_log = tmp_path / "docker-cmd.log"
    log_path = tmp_path / "run.log"

    def _timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=[str(docker), "image", "inspect", "image"], timeout=10
        )

    monkeypatch.setattr(supervisor_module, "prepare_docker_image", _timeout)
    payload = _docker_payload(
        tmp_path,
        docker,
        docker={"binary": str(docker), "image": "image", "pull": "never"},
        env={"FAKE_DOCKER_COMMAND_LOG": str(cmd_log)},
    )

    returncode = run_supervisor(
        payload["argv"],
        payload["env"],
        payload["cwd"],
        log_path,
        payload["secrets"],
        payload=payload,
    )

    assert returncode == 124
    log = log_path.read_text(encoding="utf-8")
    assert "timed out" in log
    exit_status = json.loads((tmp_path / "run.exit-status").read_text(encoding="utf-8"))
    assert exit_status["returncode"] == 124
    commands = cmd_log.read_text(encoding="utf-8") if cmd_log.exists() else ""
    assert "run -d" not in commands
