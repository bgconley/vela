from __future__ import annotations

from pathlib import Path

from vela.engine.sidecar import load_sidecar, verify_sidecar_from_system
from vela.engine.supervisor import run_supervisor


def test_docker_supervisor_writes_container_identity_sidecar(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    _write_fake_docker(docker)
    log_path = tmp_path / "run.log"
    sidecar_path = tmp_path / "run.json"
    manifest_path = tmp_path / "run.manifest.json"
    payload = {
        "runtime": "docker",
        "docker": {
            "binary": str(docker),
            "container_name": "vela-qwen",
            "image": "vllm/vllm-openai@sha256:image",
            "image_digest": "sha256:image",
            "stop_grace_seconds": 90,
        },
        "argv": [
            str(docker),
            "run",
            "-d",
            "--name",
            "vela-qwen",
            "vllm/vllm-openai@sha256:image",
            "org/model",
        ],
        "env": {},
        "cwd": str(tmp_path),
        "log_path": str(log_path),
        "manifest_path": str(manifest_path),
        "sidecar_path": str(sidecar_path),
        "exit_status_path": str(tmp_path / "run.exit-status"),
        "event_log_path": str(tmp_path / "events.ndjson"),
        "secrets": [],
        "run_id": "run-1",
        "config_name": "docker-detached",
        "config_snapshot": {"name": "docker-detached", "model": "org/model"},
        "command_hash": "sha256:docker",
        "vllm_version": None,
        "vllm_version_profile": None,
        "host": "127.0.0.1",
        "port": 8000,
        "served_model_names": ["model"],
        "exposure": "local",
        "launch_mode": "detached",
        "build_id": None,
        "build_label": None,
        "model_ref": None,
        "model_entry_id": None,
        "model_repo_id": None,
        "model_revision": None,
        "model_commit_sha": None,
    }

    returncode = run_supervisor(
        payload["argv"],
        payload["env"],
        payload["cwd"],
        log_path,
        [],
        payload=payload,
    )

    sidecar = load_sidecar(sidecar_path)
    assert returncode == 0
    assert sidecar.runtime == "docker"
    assert sidecar.docker_container_name == "vela-qwen"
    assert sidecar.docker_container_id == "container-123"
    assert sidecar.docker_image_digest == "sha256:image"
    assert sidecar.docker_stop_grace_seconds == 90
    assert "Uvicorn running" in log_path.read_text(encoding="utf-8")
    assert verify_sidecar_from_system(sidecar_path)


def _write_fake_docker(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, sys",
                "args = sys.argv[1:]",
                "if args[:2] == ['run', '-d']:",
                "    print('container-123')",
                "    raise SystemExit(0)",
                "if args[:2] == ['logs', '-f']:",
                "    print('INFO Uvicorn running on http://0.0.0.0:8000', flush=True)",
                "    raise SystemExit(0)",
                "if args[:1] == ['wait']:",
                "    print('0')",
                "    raise SystemExit(0)",
                "if args[:1] == ['inspect']:",
                "    payload = [{",
                "        'Id': 'container-123',",
                "        'Name': '/vela-qwen',",
                "        'Image': 'sha256:image',",
                "        'Config': {'Image': 'vllm/vllm-openai@sha256:image'},",
                "    }]",
                "    print(json.dumps(payload))",
                "    raise SystemExit(0)",
                "raise SystemExit(f'unexpected docker args: {args}')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
