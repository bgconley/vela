from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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


def _remote_python_probe(marker: str) -> str:
    """Extract one executable Python heredoc from the remote shell runbook."""
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")
    after_marker = script.split(marker, 1)[1]
    heredoc = after_marker.split("<<'PY'\n", 1)[1]
    return heredoc.split("\nPY\n", 1)[0]


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


def test_remote_validation_checks_out_exact_expected_revision_before_tests() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    checkout_command = 'git -C "$remote_source_path" worktree add --detach'
    compare_command = 'if [[ "$remote_head" != "$remote_expected_sha" ]]; then'
    install_command = '"$venv_python" -m pip install ".[dev]"'
    assert 'remote_expected_sha="${VELA_REMOTE_EXPECTED_SHA:-}"' in script
    assert checkout_command in script
    assert compare_command in script
    assert "remote revision mismatch" in script
    assert "exit 36" in script
    assert 'source=owned-worktree' in script
    assert 'git status --porcelain --untracked-files=all' in script
    assert 'pip install -e ".[dev]"' not in script
    assert script.index(checkout_command) < script.index(compare_command)
    assert script.index(compare_command) < script.index(install_command)


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


def test_remote_validation_script_exports_fresh_artifact_path() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert 'remote validation artifact: $artifact_path' in script
    assert "GITHUB_OUTPUT" in script
    assert 'artifact_path=$artifact_path' in script


def test_remote_validation_workflow_uploads_only_fresh_artifact() -> None:
    workflow = Path(".github/workflows/remote-validation.yml").read_text(
        encoding="utf-8"
    )

    assert "id: remote_validation" in workflow
    assert "id: validation_failure_artifact" in workflow
    assert "steps.remote_validation.outputs.artifact_path" in workflow
    assert "steps.validation_failure_artifact.outputs.artifact_path" in workflow
    assert "path: artifacts/remote-validation/*.md" not in workflow


def test_remote_validation_uses_isolated_daemon_after_install() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    install_command = '"$venv_python" -m pip install ".[dev]"'
    isolate_command = 'export VELA_AGENT_RUNTIME_DIR="$remote_agent_runtime_dir"'
    restart_command = '"$venv_bin/vela" agent restart'
    list_command = '"$venv_bin/vela" list'
    isolated_runtime = (
        'remote_agent_runtime_dir="${VELA_REMOTE_AGENT_RUNTIME_DIR:-'
        '$remote_venv/agent-runtime}"'
    )
    assert isolated_runtime in script
    assert 'trap cleanup_remote_validation EXIT' in script
    assert '--socket "$remote_agent_runtime_dir/agent.sock"' in script
    assert restart_command in script
    assert script.index(install_command) < script.index(isolate_command)
    assert script.index(isolate_command) < script.index(restart_command)
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


def test_daemon_restart_probe_cleans_owned_run_after_post_launch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed always-run probe must not strand its detached fake child."""
    from vela.transport import subprocess as transport_subprocess

    owned: dict[str, str] = {}
    clients: list[ProbeClient] = []

    class ProbeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []
            clients.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params: dict[str, object]):
            self.calls.append((method, params))
            if method == "launch":
                owned["run_id"] = str(params["run_id"])
                return {"run_id": owned["run_id"]}
            if method == "health" and self is clients[0]:
                raise RuntimeError("injected daemon probe failure")
            if method == "discover_runs":
                return {"runs": [{"run_id": owned["run_id"]}]}
            if method == "wait":
                return {"returncode": 0}
            return {}

    monkeypatch.setattr(transport_subprocess, "SubprocessTargetClient", ProbeClient)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["daemon-restart-probe", str(tmp_path), str(tmp_path / "venv-bin")],
    )

    source = _remote_python_probe('echo "== Daemon restart live-run survival =="')
    with pytest.raises(RuntimeError, match="injected daemon probe failure"):
        exec(compile(source, "<daemon-restart-probe>", "exec"), {"__name__": "probe"})

    assert len(clients) == 2
    cleanup_calls = clients[1].calls
    assert [method for method, _params in cleanup_calls] == [
        "discover_runs",
        "reattach",
        "stop",
        "wait",
    ]
    discover_params = cleanup_calls[0][1]
    assert len(discover_params["runs_dirs"]) == 1
    assert str(discover_params["runs_dirs"][0]).endswith("/runs")
    assert cleanup_calls[1][1] == {"run_id": owned["run_id"]}
    assert cleanup_calls[2][1]["run_id"] == owned["run_id"]
    assert cleanup_calls[3][1] == {"run_id": owned["run_id"]}
    assert not list(tmp_path.glob("vela-daemon-restart-*"))


def test_disconnect_reconnect_probe_cleans_owned_run_after_first_stream_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup must cover the early stream window before the normal stop finally."""
    from vela.transport import subprocess as transport_subprocess

    owned: dict[str, str] = {}
    clients: list[ProbeClient] = []

    class FailingEvents:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("injected reconnect probe failure")

        async def aclose(self) -> None:
            return None

    class ProbeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.connected = False
            self.calls: list[tuple[str, dict[str, object]]] = []
            clients.append(self)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params: dict[str, object]):
            self.calls.append((method, params))
            if method == "launch":
                owned["run_id"] = str(params["run_id"])
                return {"run_id": owned["run_id"]}
            if method == "tail_detached":
                return {}
            if method == "discover_runs":
                return {"runs": [{"run_id": owned["run_id"]}]}
            if method == "wait":
                return {"returncode": 0}
            return {}

        def subscribe(self, *_args, **_kwargs):
            return FailingEvents()

    monkeypatch.setattr(transport_subprocess, "SubprocessTargetClient", ProbeClient)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["disconnect-reconnect-probe", str(tmp_path), str(tmp_path / "venv-bin")],
    )

    source = _remote_python_probe('echo "== Disconnect/reconnect stream resume =="')
    with pytest.raises(RuntimeError, match="injected reconnect probe failure"):
        exec(
            compile(source, "<disconnect-reconnect-probe>", "exec"),
            {"__name__": "probe"},
        )

    assert len(clients) == 2
    cleanup_calls = clients[1].calls
    assert [method for method, _params in cleanup_calls] == [
        "discover_runs",
        "reattach",
        "stop",
        "wait",
    ]
    discover_params = cleanup_calls[0][1]
    assert len(discover_params["runs_dirs"]) == 1
    assert str(discover_params["runs_dirs"][0]).endswith("/runs")
    assert cleanup_calls[1][1] == {"run_id": owned["run_id"]}
    assert cleanup_calls[2][1]["run_id"] == owned["run_id"]
    assert cleanup_calls[3][1] == {"run_id": owned["run_id"]}
    assert not list(tmp_path.glob("vela-disconnect-reconnect-*"))


def test_gpu_workflow_docs_record_tested_vllm_range_and_textual_serve() -> None:
    docs = Path("docs/gpu-workflow.md").read_text(encoding="utf-8")

    assert "commit locally" in docs
    assert 'git push origin "$branch"' in docs
    assert 'export VELA_REMOTE_EXPECTED_SHA="$(git rev-parse HEAD)"' in docs
    assert "checks it out detached" in docs
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
    assert "unproven-bf16-recipe-image" in docs
    assert "BACKEND_EVIDENCE_ALLOW_UNPROVEN=1" in docs
    assert "textual serve" in docs
    assert "network/auth" in docs
    assert "controls model launches" in docs


def test_gpu_workflow_docs_record_p620_controller_to_blackbird_smoke() -> None:
    docs = Path("docs/gpu-workflow.md").read_text(encoding="utf-8")

    assert "P620-01 controller to Blackbird agent" in docs
    assert 'ssh -A -i "$VELA_LAB_SSH_KEY"' in docs
    assert "/Users/brennanconley/vibecode/infx/ubuntu24_ed25519" not in docs
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


def test_gpu_workflow_latest_validation_matches_readme() -> None:
    import re

    readme = Path("README.md").read_text(encoding="utf-8")
    gpu = Path("docs/gpu-workflow.md").read_text(encoding="utf-8")

    # README is the source of truth for the current validation commit.
    assert "Latest validation artifacts:" in readme
    latest_block = readme.split("Latest validation artifacts:", 1)[1].split("Earlier", 1)[0]

    commits = re.findall(r"Commit `([0-9a-f]{7,40})`", latest_block)
    assert commits, "README 'Latest validation artifacts' lists no commit"
    latest_commit = commits[0]

    artifacts = re.findall(
        r"`(artifacts/remote-validation/[^`]+-remote-validation\.md)`", latest_block
    )
    assert artifacts, "README 'Latest validation artifacts' lists no artifact files"

    # docs/gpu-workflow.md must not drift behind README: it must reference the
    # same latest validation commit and the same latest artifact files, so it
    # can never again label older records as "latest" (external-review finding).
    assert latest_commit in gpu, (
        "docs/gpu-workflow.md does not reference the latest validation commit "
        f"{latest_commit!r} from README; update its latest-records list."
    )
    for artifact in artifacts:
        assert artifact in gpu, (
            "docs/gpu-workflow.md is missing the latest validation artifact "
            f"{artifact!r} referenced by README."
        )


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
    assert '"$venv_python" -m pip install ".[dev]"' in remote_script
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
    assert "== Real model resume/recovery ==" in remote_script
    assert '"$venv_python" scripts/real_model_resume_check.py' in remote_script
    assert '"$remote_real_resume_config"' in remote_script
    assert 'resume_config_file="configs/${remote_real_resume_config}.yaml"' in remote_script
    assert 'resume_config_push_file="$(mktemp)"' in remote_script
    assert '("version", "transformers_version", "torch_version", "cuda_version")' in remote_script
    assert '"$venv_bin/vela" config push "$resume_config_push_file"' in remote_script
    # Hardened isolation: the stripped resume config is pushed into a throwaway
    # target config dir and the resume check reads from that same dir, so the
    # target's default config area is never dirtied; the temp dir is removed.
    assert 'resume_configs_dir="$(mktemp -d)"' in remote_script
    assert '--configs-dir "$resume_configs_dir"' in remote_script
    assert 'real_resume_args+=(--configs-dir "$resume_configs_dir")' in remote_script
    assert 'rm -rf "$resume_configs_dir"' in remote_script


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
    assert "REAL_MODEL_RECOVERY_OK" in remote_script
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
        '  echo "== Real model resume/recovery =="'
    )
    assert script.index(real_config_block) < script.index(real_resume_block)


def test_remote_validation_passes_real_build_and_model_to_process_resume_check(
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
    assert 'resume_config_runtime=' in remote_script
    assert (
        'if [[ "$resume_config_runtime" != "docker" && -n "$remote_build_spec" ]]; then'
        in remote_script
    )
    assert 'real_resume_args+=(--model-ref "$remote_model_ref")' in remote_script
    assert 'real_resume_args+=(--revision "$remote_model_revision")' in remote_script
    assert (
        '"$venv_python" scripts/real_model_resume_check.py "${real_resume_args[@]}"'
        in remote_script
    )


def test_remote_validation_skips_build_override_for_docker_resume_config() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert "command.runtime docker cannot be set with command.build" not in script
    assert 'resume_config_runtime="$(resume_config_runtime "$resume_config_file")"' in script
    assert (
        'if [[ "$resume_config_runtime" != "docker" && -n "$remote_build_spec" ]]; then'
        in script
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


def _blackbird_tiny_resume_config_payload() -> dict[str, object]:
    return {
        "name": "tiny-random-llama-detached-blackbird",
        "model": "hf-internal-testing/tiny-random-LlamaForCausalLM",
        "served_model_name": "tiny-random-llama",
        "command": {
            "runtime": "docker",
            "docker": {
                "image": (
                    "vllm/vllm-openai@sha256:"
                    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
                ),
                "env": {},
            },
        },
        "engine": {"dtype": "auto"},
        "extra_args": [],
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


def test_backend_evidence_accepts_tiny_blackbird_resume_recipe_shape() -> None:
    module = _load_backend_evidence_check()

    result = module.validate_backend_evidence(
        "tiny-random-llama-detached-blackbird",
        _blackbird_tiny_resume_config_payload(),
        "INFO tiny resume reached READY",
    )

    assert result == {
        "checked": True,
        "config_name": "tiny-random-llama-detached-blackbird",
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


def test_backend_evidence_does_not_silently_skip_unregistered_bf16_config() -> None:
    module = _load_backend_evidence_check()
    config = _blackbird_bf16_config_payload()
    config["name"] = "qwen36-27b-bf16-rp6000-renamed"

    with pytest.raises(
        module.BackendEvidenceError,
        match="unregistered backend evidence rule",
    ):
        module.validate_backend_evidence(
            "qwen36-27b-bf16-rp6000-renamed",
            config,
            "INFO BF16 recipe reached READY",
        )


def test_backend_evidence_rejects_unproven_bf16_recipe_image() -> None:
    module = _load_backend_evidence_check()
    config = _blackbird_bf16_config_payload()
    config["name"] = "qwen36-27b-bf16-rp6000-blackbird-canary"
    config["command"]["docker"]["image"] = "vllm/vllm-openai:latest"

    with pytest.raises(
        module.BackendEvidenceError,
        match="unproven-bf16-recipe-image",
    ):
        module.validate_backend_evidence(
            "qwen36-27b-bf16-rp6000-blackbird-canary",
            config,
            "INFO BF16 recipe reached READY",
        )


def test_backend_evidence_allows_unproven_bf16_recipe_image_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_evidence_check()
    config = _blackbird_bf16_config_payload()
    config["name"] = "qwen36-27b-bf16-rp6000-blackbird-canary"
    config["command"]["docker"]["image"] = "vllm/vllm-openai:latest"
    monkeypatch.setenv("BACKEND_EVIDENCE_ALLOW_UNPROVEN", "1")

    result = module.validate_backend_evidence(
        "qwen36-27b-bf16-rp6000-blackbird-canary",
        config,
        "INFO BF16 recipe reached READY",
    )

    assert result == {
        "checked": False,
        "config_name": "qwen36-27b-bf16-rp6000-blackbird-canary",
        "reason": "unproven-bf16-recipe-image",
    }


def test_backend_evidence_rejects_unproven_fp8_recipe_without_anchors() -> None:
    module = _load_backend_evidence_check()
    config = _blackbird_fp8_config_payload()
    config["name"] = "qwen36-27b-fp8-kvfp8-rp6000-blackbird-canary"
    config["command"]["docker"]["image"] = "vllm/vllm-openai:latest"
    config["command"]["docker"]["env"].pop("FLASHINFER_CUDA_ARCH_LIST", None)

    with pytest.raises(
        module.BackendEvidenceError,
        match="unproven-fp8-recipe-anchors",
    ):
        module.validate_backend_evidence(
            "qwen36-27b-fp8-kvfp8-rp6000-blackbird-canary",
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


def test_real_model_resume_check_reconnects_without_restarting_ssh_target_daemon(
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

    assert module._restart_target_agent(target) == "ssh-reconnect"


@pytest.mark.asyncio
async def test_real_model_resume_check_cleans_up_its_run_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_real_model_resume_check()
    cleanup_calls: list[tuple[str, str, tuple[str, ...], bool]] = []

    async def fail_validation(*_args, cleanup_context, **_kwargs) -> None:
        cleanup_context.runs_dirs = ["/custom/real-model-runs"]
        cleanup_context.launch_attempted = True
        raise RuntimeError("cursor replay failed")

    async def cleanup(
        target_name: str,
        run_id: str,
        *,
        runs_dirs: list[str],
        launch_attempted: bool,
    ) -> str:
        cleanup_calls.append(
            (target_name, run_id, tuple(runs_dirs), launch_attempted)
        )
        return "stopped:returncode=0"

    monkeypatch.setattr(module, "_run_validation", fail_validation)
    monkeypatch.setattr(module, "_cleanup_failed_run", cleanup)

    with pytest.raises(RuntimeError, match="cursor replay failed"):
        await module._run(
            "real-config",
            target_name="blackbird",
            timeout=30.0,
            build=None,
            model_ref=None,
            revision=None,
        )

    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][0] == "blackbird"
    assert cleanup_calls[0][1].startswith("real-resume-")
    assert cleanup_calls[0][2] == ("/custom/real-model-runs",)
    assert cleanup_calls[0][3] is True


@pytest.mark.asyncio
async def test_real_model_resume_cleanup_rediscovers_and_stops_only_owned_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_real_model_resume_check()

    class CleanupClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params: dict[str, object]):
            self.calls.append((method, params))
            if method == "discover_runs":
                return {"runs": [{"run_id": "owned-run"}]}
            if method == "wait":
                return {"returncode": 0}
            return {}

    client = CleanupClient()
    target = TargetConfig(name="blackbird", transport=TransportKind.SSH, host="gpu")
    monkeypatch.setattr(module, "_new_client", lambda _name: (target, client))

    result = await module._cleanup_failed_run(
        "blackbird",
        "owned-run",
        runs_dirs=["/custom/real-model-runs"],
        launch_attempted=True,
    )

    assert result == "stopped:returncode=0"
    assert [method for method, _params in client.calls] == [
        "discover_runs",
        "reattach",
        "stop",
        "wait",
    ]
    assert client.calls[0][1] == {"runs_dirs": ["/custom/real-model-runs"]}
    stop_params = dict(client.calls[2][1])
    assert stop_params == {
        "run_id": "owned-run",
        "interrupt_timeout": 2,
        "terminate_timeout": 2,
    }
    assert client.connected is False


@pytest.mark.asyncio
async def test_real_model_resume_cleanup_not_found_after_launch_is_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_real_model_resume_check()

    async def fail_validation(*_args, cleanup_context, **_kwargs) -> None:
        cleanup_context.runs_dirs = ["/custom/real-model-runs"]
        cleanup_context.launch_attempted = True
        raise RuntimeError("launch response was lost")

    async def cleanup(
        _target_name: str,
        _run_id: str,
        *,
        runs_dirs: list[str],
        launch_attempted: bool,
    ) -> str:
        assert runs_dirs == ["/custom/real-model-runs"]
        assert launch_attempted is True
        return "not-found-after-launch"

    monkeypatch.setattr(module, "_run_validation", fail_validation)
    monkeypatch.setattr(module, "_cleanup_failed_run", cleanup)

    with pytest.raises(RuntimeError, match="launch response was lost"):
        await module._run(
            "real-config",
            target_name="blackbird",
            timeout=30.0,
            build=None,
            model_ref=None,
            revision=None,
        )

    stderr = capsys.readouterr().err
    assert "REAL_MODEL_CLEANUP_WARNING" in stderr
    assert "result=not-found-after-launch" in stderr
    assert "REAL_MODEL_CLEANUP_OK" not in stderr


@pytest.mark.asyncio
async def test_real_model_resume_cleanup_does_not_claim_missing_launched_run_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_real_model_resume_check()

    class MissingRunClient:
        connected = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def call(self, method: str, params: dict[str, object]):
            self.calls.append((method, params))
            return {"runs": []}

    client = MissingRunClient()
    target = TargetConfig(name="blackbird", transport=TransportKind.SSH, host="gpu")
    monkeypatch.setattr(module, "_new_client", lambda _name: (target, client))

    result = await module._cleanup_failed_run(
        "blackbird",
        "missing-owned-run",
        runs_dirs=["/custom/real-model-runs"],
        launch_attempted=True,
    )

    assert result == "not-found-after-launch"
    assert client.calls == [
        ("discover_runs", {"runs_dirs": ["/custom/real-model-runs"]})
    ]
    assert client.connected is False


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
    # Phase-9 (D4): the zombie daily cron is killed; the workflow is dispatch-only.
    assert "schedule:" not in text
    assert "cron:" not in text
    assert "concurrency:" in text
    assert "runner_label" in text
    assert 'default: "self-hosted"' in text
    assert "remote_target" in text
    assert "real_resume_config" in text
    assert "gated_model_repo" in text
    # Personal defaults are scrubbed: values live in repo variables, not the file.
    assert "bgconley" not in text
    assert "/home/bgconley" not in text
    assert "vars.VELA_REMOTE_HOST" in text
    assert "vars.VELA_REMOTE_REAL_RESUME_CONFIG" in text
    assert "Validation host/path not configured" in text
    assert "VELA_REMOTE_REAL_RESUME_CONFIG" in text
    assert "VELA_REMOTE_GATED_MODEL_REPO" in text
    assert "VELA_REMOTE_TARGET" in text
    assert "ssh-agent -s" in text
    assert "ssh-add \"$key_path\"" in text
    assert "VELA_SSH_OPTS=-A -i $key_path" in text
    assert "scripts/run_remote_tests.sh" in text
    assert "VELA_REMOTE_ARTIFACT_DIR" in text
    assert "VELA_REMOTE_BRANCH: ${{ github.ref_name }}" in text
    assert "VELA_REMOTE_EXPECTED_SHA: ${{ github.sha }}" in text
    assert (
        "secrets.VELA_REMOTE_SSH_KEY || "
        "secrets.VLLM_LOADER_REMOTE_SSH_KEY" in text
    )
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


def test_remote_validation_supports_exact_branch_revision_override() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")
    assert 'remote_branch="${VELA_REMOTE_BRANCH:-main}"' in script
    assert 'remote_expected_sha="${VELA_REMOTE_EXPECTED_SHA:-}"' in script
    assert 'VELA_REMOTE_BRANCH=$remote_branch_local' in script
    assert 'VELA_REMOTE_EXPECTED_SHA=$remote_expected_sha_local' in script
    assert (
        '"refs/heads/$remote_branch:refs/remotes/origin/$remote_branch"'
        in script
    )
    assert 'git -C "$remote_source_path" worktree add --detach' in script
    assert 'remote_head="$(git rev-parse HEAD)"' in script
    assert 'REMOTE_REVISION_OK expected=$remote_expected_sha actual=$remote_head' in script


def test_remote_validation_fails_closed_on_remote_revision_mismatch(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True)
    (source / "README").write_text("revision proof\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Vela Test",
            "-c",
            "user.email=vela@example.invalid",
            "commit",
            "-m",
            "seed",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", str(origin)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "push", "-u", "origin", "main"],
        check=True,
    )
    subprocess.run(["git", "clone", "-b", "main", str(origin), str(remote)], check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\nshift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        VELA_REMOTE_EXPECTED_SHA="0" * 40,
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "fake-host", str(remote)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 36
    assert "remote revision mismatch" in result.stderr


def test_remote_validation_installs_exact_sha_from_clean_owned_worktree(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True)
    (source / "README").write_text("certified revision\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Vela Test",
            "-c",
            "user.email=vela@example.invalid",
            "commit",
            "-m",
            "seed",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", str(origin)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "push", "-u", "origin", "main"],
        check=True,
    )
    subprocess.run(["git", "clone", "-b", "main", str(origin), str(remote)], check=True)
    expected_sha = subprocess.check_output(
        ["git", "-C", str(remote), "rev-parse", "HEAD"], text=True
    ).strip()

    # A tracked edit and untracked source poison the reusable controller checkout.
    # The validation must neither execute them nor destroy them.
    (remote / "README").write_text("dirty controller checkout\n", encoding="utf-8")
    (remote / "POISON").write_text("must not enter validation\n", encoding="utf-8")

    fake_venv = tmp_path / "venv"
    fake_bin = fake_venv / "bin"
    fake_bin.mkdir(parents=True)
    checkout_capture = tmp_path / "checkout-path"
    fake_python = fake_bin / "python"
    fake_python.write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" && "${3:-}" == "--version" ]]; then
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" && "${3:-}" == "install" ]]; then
  printf '%s\n' "$PWD" > "$CHECKOUT_CAPTURE"
  [[ ! -e POISON ]] || exit 91
  [[ "$(cat README)" == "certified revision" ]] || exit 92
  exit 73
fi
exit 70
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/usr/bin/env bash\nshift\nexec \"$@\"\n", encoding="utf-8")
    ssh.chmod(0o755)
    env = _script_test_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        CHECKOUT_CAPTURE=str(checkout_capture),
        VELA_REMOTE_EXPECTED_SHA=expected_sha,
        VELA_REMOTE_VENV=str(fake_venv),
    )

    result = subprocess.run(
        ["bash", "scripts/run_remote_tests.sh", "fake-host", str(remote)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 73, result.stdout + result.stderr
    validation_checkout = Path(checkout_capture.read_text(encoding="utf-8").strip())
    assert validation_checkout != remote
    assert not validation_checkout.exists(), "owned worktree must be removed on failure"
    assert (remote / "README").read_text(encoding="utf-8") == "dirty controller checkout\n"
    assert (remote / "POISON").is_file()


def test_fast_remote_validation_profile_clears_real_resume_inputs() -> None:
    workflow = Path(".github/workflows/remote-validation.yml").read_text(
        encoding="utf-8"
    )

    fast = 'if [[ "$VALIDATION_PROFILE" == "fast" ]]; then'
    clear_resume = "unset VELA_REMOTE_REAL_RESUME_CONFIG"
    invoke = 'bash scripts/run_remote_tests.sh "${args[@]}"'
    assert workflow.index(fast) < workflow.index(clear_resume) < workflow.index(invoke)
