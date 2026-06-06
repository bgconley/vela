from __future__ import annotations

import json
from pathlib import Path

from vela.engine.supervisor import run_supervisor


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
