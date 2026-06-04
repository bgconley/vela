from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _script_test_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("VLLM_LOADER_REMOTE_") or key == "VLLM_LOADER_SSH_OPTS":
            env.pop(key, None)
    env.update(overrides)
    return env


def test_remote_validation_uses_textual_smoke_for_real_config() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert "after local commit/push; the GPU node pulls from git" in script
    assert "sample_gpus" in script
    assert "vllm --version" in script
    assert "vllm serve --help" in script
    assert '"$venv_bin/vllm-loader" smoke-tui "$real_config"' in script
    assert '"$venv_bin/vllm-loader" smoke "$real_config"' not in script
    assert 'vllm-loader run "$real_config"' not in script
    assert 'remote_venv="${4:-/tank/venvs/lab-tui}"' in script
    assert '"$venv_python" -m pip --version' in script
    assert "install python3-venv/ensurepip or set VLLM_LOADER_REMOTE_PYTHON" in script


def test_remote_validation_pulls_committed_git_state_before_tests() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    pull_command = 'git -C "$remote_path" pull --ff-only origin main'
    install_command = '"$venv_python" -m pip install -e ".[dev]"'
    assert pull_command in script
    assert script.index(pull_command) < script.index(install_command)


def test_remote_validation_restarts_daemon_after_install() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    install_command = '"$venv_python" -m pip install -e ".[dev]"'
    restart_command = '"$venv_bin/vllm-loader" agent restart'
    list_command = '"$venv_bin/vllm-loader" list'
    assert restart_command in script
    assert script.index(install_command) < script.index(restart_command)
    assert script.index(restart_command) < script.index(list_command)


def test_remote_validation_exercises_live_run_daemon_restart() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert "== Daemon restart live-run survival ==" in script
    assert "SubprocessTargetClient" in script
    assert '"agent", "connect"' in script
    assert 'await client.call(\n            "launch"' in script
    assert 'subprocess.run([str(venv_bin / "vllm-loader"), "agent", "restart"]' in script
    assert 'await client.call("discover_runs"' in script
    assert 'await client.call("reattach"' in script
    assert 'await client.call(\n            "stop"' in script
    assert 'await client.call("wait", {"run_id": run_id})' in script
    assert "DAEMON_RESTART_LIVE_RUN_OK" in script


def test_remote_validation_exercises_disconnect_reconnect_resume() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert "== Disconnect/reconnect stream resume ==" in script
    assert "disconnect-reconnect-fake" in script
    assert "tail_detached" in script
    assert '"start_position": 0' in script
    assert '"resume_from": {' in script
    assert '"log_inode": first_cursor["log_inode"]' in script
    assert '"byte_offset": first_cursor["byte_offset"]' in script
    assert 'await client.call("discover_runs", {"runs_dirs": [str(runs_dir)]})' in script
    assert 'await client.call("reattach", {"run_id": run_id})' in script
    assert "DISCONNECT_RECONNECT_RESUME_OK" in script


def test_gpu_workflow_docs_record_tested_vllm_range_and_textual_serve() -> None:
    docs = Path("docs/gpu-workflow.md").read_text(encoding="utf-8")

    assert "commit locally" in docs
    assert "git push origin main" in docs
    assert "git pull --ff-only origin main" in docs
    assert "rsync_to_gpu.sh" not in docs
    assert "qwen36-27b-fp8-kvfp8-rp6000-blackbird" in docs
    assert "10.25.0.51" in docs
    assert "RTX PRO 6000 Blackwell" in docs
    assert "vLLM `0.20.2rc1.dev9+g01d4d1ad3`" in docs
    assert "v0.19.1rc1.dev119+gba4a78eb5" in docs
    assert "vLLM 0.19" in docs
    assert "vllm-loader smoke-tui" in docs
    assert "VLLM_LOADER_REMOTE_BUILD_SPEC" in docs
    assert "VLLM_LOADER_REMOTE_MODEL_REPO" in docs
    assert "textual serve" in docs
    assert "network/auth" in docs
    assert "controls model launches" in docs


def test_gpu_workflow_docs_record_p620_controller_to_blackbird_smoke() -> None:
    docs = Path("docs/gpu-workflow.md").read_text(encoding="utf-8")

    assert "P620-01 controller to Blackbird agent" in docs
    assert "ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519" in docs
    assert "vllm-loader targets test blackbird" in docs
    assert (
        "vllm-loader smoke-tui qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird"
        in docs
    )
    assert (
        "artifacts/remote-validation/2026-06-04-p620-blackbird-eb2a116-remote-validation.md"
        in docs
    )
    assert "VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG" in docs


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
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_REMOTE_TIMEOUT="2400",
    )

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


def test_remote_validation_can_target_nested_agent_workflow(tmp_path: Path) -> None:
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
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_REMOTE_TARGET="blackbird",
        VLLM_LOADER_REMOTE_BUILD_SPEC="vllm==0.11.2",
        VLLM_LOADER_REMOTE_MODEL_ID="real-model-smoke",
        VLLM_LOADER_REMOTE_MODEL_REPO="hf-internal-testing/tiny-random-LlamaForCausalLM",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/run_remote_tests.sh",
            "p620-controller",
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
    assert args[:5] == [
        "p620-controller",
        "env",
        "VLLM_LOADER_REMOTE_TARGET=blackbird",
        "bash",
        "-s",
    ]
    expected_snippets = [
        'target_args=(--target "$remote_target")',
        '"$venv_bin/vllm-loader" build add "${target_args[@]}"',
        '"$venv_bin/vllm-loader" build verify "$remote_build_label" '
        '"${target_args[@]}"',
        '"$venv_bin/vllm-loader" model pin "$remote_model_id" '
        '"${target_args[@]}"',
        '"$venv_bin/vllm-loader" model download "$remote_model_ref" '
        '"${target_args[@]}"',
        '"$venv_bin/vllm-loader" model verify "$remote_model_ref" '
        '"${target_args[@]}"',
        '"$venv_bin/vllm-loader" preview "$real_config" "${target_args[@]}"',
        'timeout "$remote_timeout" "$venv_bin/vllm-loader" smoke-tui '
        '"$real_config" "${target_args[@]}"',
    ]
    for snippet in expected_snippets:
        assert snippet in remote_script


def test_remote_validation_can_run_real_model_resume_check(tmp_path: Path) -> None:
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
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_REMOTE_TARGET="blackbird",
        VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG="qwen-real",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "p620-controller", "/srv/lab-tui"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    assert args[:5] == [
        "p620-controller",
        "env",
        "VLLM_LOADER_REMOTE_TARGET=blackbird",
        "VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG=qwen-real",
        "bash",
    ]
    assert "== Real model resume/daemon restart ==" in remote_script
    assert '"$venv_python" scripts/real_model_resume_check.py' in remote_script
    assert '"$remote_real_resume_config"' in remote_script


def test_real_model_resume_check_discovers_run_before_reconnect_reattach() -> None:
    script = Path("scripts/real_model_resume_check.py").read_text(encoding="utf-8")

    reconnect_marker = "await asyncio.sleep(5.0)"
    reconnect_at = script.index(reconnect_marker)
    discover = 'await client.call("discover_runs", discover_params)'
    reattach = 'await client.call("reattach", {"run_id": run_id})'

    assert script.index(discover, reconnect_at) < script.index(reattach, reconnect_at)


def test_remote_validation_accepts_pytest_args_override(tmp_path: Path) -> None:
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
    pytest_args = "-q tests/test_remote_workflow.py -k target_nested"
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_REMOTE_PYTEST_ARGS=pytest_args,
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "controller-host", "/srv/lab-tui"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    assert args[:5] == [
        "controller-host",
        "env",
        "VLLM_LOADER_REMOTE_PYTEST_ARGS=-q\\ "
        "tests/test_remote_workflow.py\\ -k\\ target_nested",
        "bash",
        "-s",
    ]
    assert 'remote_pytest_args="${VLLM_LOADER_REMOTE_PYTEST_ARGS:--q}"' in remote_script
    assert 'read -r -a pytest_args <<< "$remote_pytest_args"' in remote_script
    assert '"$venv_python" -m pytest "${pytest_args[@]}"' in remote_script


def test_remote_validation_can_execute_real_build_and_model_jobs(tmp_path: Path) -> None:
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
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_REMOTE_BUILD_SPEC="vllm==0.11.2",
        VLLM_LOADER_REMOTE_BUILD_LABEL="real-build-smoke",
        VLLM_LOADER_REMOTE_MODEL_ID="real-model-smoke",
        VLLM_LOADER_REMOTE_MODEL_REPO="hf-internal-testing/tiny-random-LlamaForCausalLM",
        VLLM_LOADER_REMOTE_MODEL_REVISION="main",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "gpu-host", "/srv/lab-tui"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    empty = "__VLLM_LOADER_EMPTY__"
    assert args[-12:] == [
        "/srv/lab-tui",
        "1800",
        "auto",
        "/tank/venvs/lab-tui",
        empty,
        "pip",
        "vllm==0.11.2",
        "real-build-smoke",
        "real-model-smoke",
        "hf-internal-testing/tiny-random-LlamaForCausalLM",
        empty,
        "main",
    ]
    assert 'remote_build_method="$(_remote_arg_or_empty "${6:-pip}")"' in remote_script
    assert '"$venv_bin/vllm-loader" build add' in remote_script
    assert '--method "$remote_build_method"' in remote_script
    assert '--spec "$remote_build_spec"' in remote_script
    assert '"$venv_bin/vllm-loader" build verify "$remote_build_label"' in remote_script
    assert '"$venv_bin/vllm-loader" model pin "$remote_model_id"' in remote_script
    assert '--repo-id "$remote_model_repo"' in remote_script
    assert '"$venv_bin/vllm-loader" model download "$remote_model_ref"' in remote_script
    assert '"$venv_bin/vllm-loader" model verify "$remote_model_ref"' in remote_script


def test_remote_validation_writes_dated_artifact(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "ssh-capture"
    artifact_dir = tmp_path / "artifacts"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'printf "%s\\n" "$@" > "${SSH_CAPTURE}.args"',
                'cat > "${SSH_CAPTURE}.stdin"',
                'echo "REMOTE_OK from $1"',
            ]
        ),
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_REMOTE_ARTIFACT_DIR=str(artifact_dir),
        VLLM_LOADER_REMOTE_ARTIFACT_NAME="2026-06-04-gpu-host-smoke.md",
    )

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
    artifact = artifact_dir / "2026-06-04-gpu-host-smoke.md"
    assert artifact.exists()
    artifact_text = artifact.read_text(encoding="utf-8")
    assert "# vLLM Loader Remote Validation" in artifact_text
    assert "Host: `gpu-host`" in artifact_text
    assert "Remote path: `/srv/lab-tui`" in artifact_text
    assert "Real config: `real-config`" in artifact_text
    assert "REMOTE_OK from gpu-host" in artifact_text
    assert "Exit status: `0`" in artifact_text
    assert f"remote validation artifact: {artifact}" in result.stderr


def test_remote_validation_model_only_uses_nonempty_ssh_placeholders(
    tmp_path: Path,
) -> None:
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
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_REMOTE_MODEL_ID="real-model-smoke",
        VLLM_LOADER_REMOTE_MODEL_REPO="sshleifer/tiny-gpt2",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "gpu-host", "/srv/lab-tui"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    empty = "__VLLM_LOADER_EMPTY__"
    assert args[-12:] == [
        "/srv/lab-tui",
        "1800",
        "auto",
        "/tank/venvs/lab-tui",
        empty,
        "pip",
        empty,
        "remote-smoke-build",
        "real-model-smoke",
        "sshleifer/tiny-gpt2",
        empty,
        empty,
    ]
    assert 'empty_arg="__VLLM_LOADER_EMPTY__"' in remote_script
    assert "_remote_arg_or_empty" in remote_script


def test_manual_remote_validation_workflow_executes_script_and_uploads_artifact() -> None:
    workflow = Path(".github/workflows/remote-validation.yml")

    assert workflow.exists()
    text = workflow.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "schedule:" in text
    assert "concurrency:" in text
    assert "runner_label" in text
    assert 'default: "self-hosted"' in text
    assert "remote_target" in text
    assert "real_resume_config" in text
    assert "VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG" in text
    assert "VLLM_LOADER_REMOTE_TARGET" in text
    assert "ssh-agent -s" in text
    assert "ssh-add \"$key_path\"" in text
    assert "VLLM_LOADER_SSH_OPTS=-A -i $key_path" in text
    assert "scripts/run_remote_tests.sh" in text
    assert "VLLM_LOADER_REMOTE_ARTIFACT_DIR" in text
    assert "actions/upload-artifact" in text
    assert "remote-validation-artifacts" in text


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
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_SSH_OPTS="-i /tmp/gpu-key -o BatchMode=yes",
    )

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
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SSH_CAPTURE=str(capture),
        VLLM_LOADER_REMOTE_VENV="/tank/venvs/custom-lab-tui",
    )

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
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        RSYNC_CAPTURE=str(capture),
        VLLM_LOADER_SSH_OPTS="-i /tmp/gpu-key -o BatchMode=yes",
    )

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
