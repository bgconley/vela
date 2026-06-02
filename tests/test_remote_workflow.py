from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_remote_validation_uses_textual_smoke_for_real_config() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert "sample_gpus" in script
    assert "vllm --version" in script
    assert "vllm serve --help" in script
    assert '"$venv_bin/vllm-loader" smoke-tui "$real_config"' in script
    assert '"$venv_bin/vllm-loader" smoke "$real_config"' not in script
    assert 'vllm-loader run "$real_config"' not in script
    assert 'remote_venv="${4:-/tank/venvs/lab-tui}"' in script
    assert '"$venv_python" -m pip --version' in script
    assert "install python3-venv/ensurepip or set VLLM_LOADER_REMOTE_PYTHON" in script


def test_gpu_workflow_docs_record_tested_vllm_range_and_textual_serve() -> None:
    docs = Path("docs/gpu-workflow.md").read_text(encoding="utf-8")

    assert "v0.19.1rc1.dev119+gba4a78eb5" in docs
    assert "vLLM 0.19" in docs
    assert "vllm-loader smoke-tui" in docs
    assert "textual serve" in docs
    assert "network/auth" in docs
    assert "controls model launches" in docs


def test_remote_validation_forwards_timeout_override_to_ssh_script(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "ssh-capture"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'printf "%s\\n" "$@" > "${SSH_CAPTURE}.args"',
                'cat > "${SSH_CAPTURE}.stdin"',
            ]
        ),
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SSH_CAPTURE": str(capture),
        "VLLM_LOADER_REMOTE_TIMEOUT": "2400",
    }

    result = subprocess.run(
        [
            "bash",
            "scripts/run_remote_tests.sh",
            "gpu-host",
            "/srv/lab-tui",
            "real-config",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    assert args[-5:] == [
        "/srv/lab-tui",
        "2400",
        "auto",
        "/tank/venvs/lab-tui",
        "real-config",
    ]
    assert 'remote_timeout="$2"' in remote_script
    assert 'remote_python="${3:-auto}"' in remote_script
    assert 'remote_venv="${4:-/tank/venvs/lab-tui}"' in remote_script
    assert '"$venv_python" -m pip install -e ".[dev]"' in remote_script
    assert 'export PATH="$venv_bin:$PATH"' in remote_script
    assert (
        'timeout "$remote_timeout" "$venv_bin/vllm-loader" smoke-tui "$real_config"'
        in remote_script
    )


def test_remote_validation_accepts_ssh_options_for_gpu_keys(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "ssh-capture"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'printf "%s\\n" "$@" > "${SSH_CAPTURE}.args"',
                'cat > "${SSH_CAPTURE}.stdin"',
            ]
        ),
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SSH_CAPTURE": str(capture),
        "VLLM_LOADER_SSH_OPTS": "-i /tmp/gpu-key -o BatchMode=yes",
    }

    subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "gpu-host", "/srv/lab-tui"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    assert args[:5] == ["-i", "/tmp/gpu-key", "-o", "BatchMode=yes", "gpu-host"]
    assert args[-4:] == ["/srv/lab-tui", "1800", "auto", "/tank/venvs/lab-tui"]


def test_remote_validation_accepts_zfs_venv_override(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "ssh-capture"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'printf "%s\\n" "$@" > "${SSH_CAPTURE}.args"',
                'cat > "${SSH_CAPTURE}.stdin"',
            ]
        ),
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SSH_CAPTURE": str(capture),
        "VLLM_LOADER_REMOTE_VENV": "/tank/venvs/custom-lab-tui",
    }

    subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "gpu-host", "/srv/lab-tui"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    assert args[-4:] == ["/srv/lab-tui", "1800", "auto", "/tank/venvs/custom-lab-tui"]


def test_rsync_to_gpu_accepts_ssh_options_for_gpu_keys(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "rsync.args"
    rsync = bin_dir / "rsync"
    rsync.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'printf "%s\\n" "$@" > "${RSYNC_CAPTURE}"',
            ]
        ),
        encoding="utf-8",
    )
    rsync.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RSYNC_CAPTURE": str(capture),
        "VLLM_LOADER_SSH_OPTS": "-i /tmp/gpu-key -o BatchMode=yes",
    }

    subprocess.run(
        ["bash", "scripts/rsync_to_gpu.sh", "gpu-host:/srv/lab-tui"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    rsh_index = args.index("--rsh")
    assert args[rsh_index + 1] == "ssh -i /tmp/gpu-key -o BatchMode=yes"
