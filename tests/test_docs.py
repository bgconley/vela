from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _load_script(path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, Path(path).resolve())
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tui_doc_matches_bindings() -> None:
    # 8.2: docs/tui.md is generated from the TUI's declared BINDINGS. Regenerate the
    # same content in memory and diff it against the committed file, so a bindings
    # change without a docs regen fails here (drift-proof, same idea as the other
    # generated-content pins).
    gen = _load_script("scripts/gen_tui_docs.py", "gen_tui_docs_test")
    expected = gen.render_tui_docs()
    committed = _read("docs/tui.md")
    assert committed == expected, (
        "docs/tui.md is stale — regenerate with `python3 scripts/gen_tui_docs.py`"
    )


def test_gen_tui_docs_stdout_prints_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Phase-9: `--stdout` prints the rendered doc to stdout and must NOT rewrite the
    # committed file (it was previously ignored — main() always wrote).
    gen = _load_script("scripts/gen_tui_docs.py", "gen_tui_docs_stdout_test")
    fake_doc = tmp_path / "tui.md"
    monkeypatch.setattr(gen, "DOC_PATH", fake_doc)

    gen.main(["--stdout"])

    captured = capsys.readouterr()
    assert captured.out == gen.render_tui_docs()
    assert not fake_doc.exists()  # --stdout writes nothing


def test_gen_tui_docs_default_writes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default (no --stdout) writes docs/tui.md with the rendered content.
    gen = _load_script("scripts/gen_tui_docs.py", "gen_tui_docs_write_test")
    fake_doc = tmp_path / "tui.md"
    monkeypatch.setattr(gen, "DOC_PATH", fake_doc)

    gen.main([])

    assert fake_doc.read_text(encoding="utf-8") == gen.render_tui_docs()


def test_troubleshooting_doc_covers_every_remediation_kind() -> None:
    # 8.3: docs/troubleshooting.md must carry a section for every remediation kind the
    # code can emit. Extract the kind labels straight from remediation.py's source so a
    # new ErrorRemediation(label=...) without a matching doc section fails here.
    import re

    source = _read("src/vela/remediation.py")
    labels = set(re.findall(r'label="([A-Z_]+)"', source))
    assert labels, "no remediation labels found — the label= regex drifted from the source"

    doc = _read("docs/troubleshooting.md")
    missing = sorted(label for label in labels if label not in doc)
    assert not missing, f"docs/troubleshooting.md is missing remediation kinds: {missing}"


def test_troubleshooting_doc_covers_launch_lifecycle_surfaces() -> None:
    # 8.3: the doc is also the canonical operator answer for the Phase-5/6/7 launch and
    # discovery surfaces, quoted verbatim from the code.
    doc = _read("docs/troubleshooting.md")

    for phrase in (
        # Prelaunch cache gate (5.2).
        "model-not-cached",
        "require_cached_models",
        "--require-cached",
        # Docker pull timeout (5.1).
        "VELA_DOCKER_PULL_TIMEOUT_SECONDS",
        "image-pull-timeout",
        # Disk-headroom precheck (5.9).
        "insufficient-disk",
        "insufficient disk for model download",
        # Gated model auth.
        "HF_TOKEN",
        # Daemon start log (6.4) + unknown-config searched dirs / daemon cwd (6.3).
        "agent-start.err",
        "Unknown config",
        # The CLI's remediation banner shape.
        "vela targets bootstrap",
        "vela agent gen-token --install",
    ):
        assert phrase in doc, f"docs/troubleshooting.md missing surface: {phrase!r}"


def test_readme_covers_new_contributor_v1_paths() -> None:
    text = _read("README.md")

    for phrase in (
        "## Quickstart",
        "## Remote Targets",
        "## Config Schema",
        "## Build Methods",
        "## Model Registry",
        "## Agent/RPC Overview",
        "## Tested Matrix",
    ):
        assert phrase in text

    # 8.1: two explicit golden-path quickstarts — installed tool vs cloned repo.
    assert "### Installed tool" in text
    assert "### Cloned repo" in text
    # The cloned-repo demo works because the repo ships ./configs (the fake-child
    # deployment); the README must say so AND say to run from the repo root.
    assert "fake-child" in text
    assert "run from the repo root" in text
    assert "./configs" in text
    # 8.1: the remote-target golden path bootstraps the agent over SSH; a hand-edited
    # targets.yaml is demoted to reference.
    assert "vela targets bootstrap gpu-node --host user@host --install" in text
    assert "vela targets test gpu-node" in text
    # Config discovery: the configs/ SUBDIR of ~/.config/vela (not the dir itself),
    # honouring XDG_CONFIG_HOME (6.5); the default target resolves via VELA_TARGET (7.3).
    assert "~/.config/vela/configs" in text
    assert "XDG_CONFIG_HOME" in text
    assert "VELA_TARGET" in text
    # Phase-9: the TUI key reference and the troubleshooting guide were orphaned
    # (linked from nowhere); the README now points to both.
    assert "docs/tui.md" in text
    assert "docs/troubleshooting.md" in text


def test_oxcart_local_validation_runbook_pins_release_proof_and_safety_contracts() -> None:
    workflow = _read("docs/gpu-workflow.md")
    runbook = _read("docs/oxcart-local-validation.md")

    assert "docs/oxcart-local-validation.md" in workflow
    for phrase in (
        "HF_HOME=/tank/ai/models/qwen36-27b-fp8/hf-cache",
        "HF_HUB_CACHE=/tank/ai/models/qwen36-27b-fp8/hf-cache/hub",
        "huggingface_hub.constants.HF_HUB_CACHE",
        "unset HF_HUB_OFFLINE",
        "scripts/oxcart_live_guard.py preflight --snapshot",
        "127.0.0.1:8815:127.0.0.1:8815",
        "bgconley@10.25.0.50",
        "/tank/work/validation/vela-oxcart-pilot-$RUN_ID",
        "textual-serve==1.1.3",
        '"Authorization": "Bearer EMPTY"',
        "scripts/backend_evidence_check.py",
        "/health",
        "/v1/models",
        "data:image/png;base64,",
        "LEFT=RED; RIGHT=GREEN",
        "required_hostname",
        "manifest.json",
        "checksums.sha256",
        "scripts/oxcart_live_guard.py postflight",
        "scripts/oxcart_live_guard.py cleanup",
        "Blackbird is not contacted",
        "shared daemon",
    ):
        assert phrase in runbook, f"Oxcart runbook missing safety/proof surface: {phrase!r}"


def test_oxcart_runbook_fail_closes_attached_run_and_ui_lifecycle() -> None:
    runbook = _read("docs/oxcart-local-validation.md")

    # A live attached UI launch must come from the configured artifact authority,
    # not an ambiguous history projection that also contains stopped rows.
    for phrase in (
        "from vela.engine.sidecar import load_sidecar, verify_sidecar_from_system",
        'configured_runs_dir = Path(os.environ["RUNS_DIR"])',
        "configured_runs_dir != expected_runs_dir",
        'sidecar.config_name != os.environ["PROFILE"]',
        'sidecar.launch_mode != "attached"',
        'path.with_suffix(".exit-status").exists()',
        "verify_sidecar_from_system(path)",
        "unreadable sidecar candidate in exact RUNS_DIR",
        "expected exactly one verified attached profile run",
    ):
        assert phrase in runbook
    assert '"$VENV/bin/vela" runs list' not in runbook

    # Assignment must receive command-substitution failure before export can mask
    # the status under set -e. Pin the contract for both live launches.
    assert 'RUN1_ID="$(identify_attached_run)"\nexport RUN1_ID' in runbook
    assert 'RUN2_ID="$(identify_attached_run)"\nexport RUN2_ID' in runbook
    assert "export RUN1_ID=\"$(" not in runbook
    assert "export RUN2_ID=\"$(" not in runbook
    assert not [
        line for line in runbook.splitlines() if line.startswith("export ") and "$(" in line
    ]

    # The visible UI server is one exact exec-owned process, not a tee pipeline.
    # Browser disconnect and full process identity checks precede the signal; all
    # recorded sessions and the listener are rechecked before root deletion.
    for phrase in (
        'exec "$VENV/bin/python" -c',
        "navigate the visible browser to `about:blank`",
        '"cwd": process.cwd()',
        '"cmdline": process.cmdline()',
        "process.children(recursive=True)",
        'kill -TERM "$UI_PID"',
        'ss -H -ltn "sport = :$UI_PORT"',
        "children_absent_before_stop",
        "server_identity_absent_after_stop",
        'for session in ("save", "run1", "run2", "wrong-host")',
    ):
        assert phrase in runbook
    assert 'UI_PID="$("$VENV/bin/python"' in runbook
    assert ')"\nexport UI_PID' in runbook
    assert '2>&1 | tee "$EVIDENCE/textual-serve' not in runbook
    assert runbook.rfind('ss -H -ltn "sport = :$UI_PORT"') < runbook.rfind(
        "worktree remove --force"
    )

    # `agent status` intentionally exits nonzero after a successful final stop.
    # Its JSON and return code remain evidence without aborting the cleanup shell.
    for phrase in (
        "DAEMON_STATUS_RC=0",
        "|| DAEMON_STATUS_RC=$?",
        '"$EVIDENCE/owned-daemon-after.rc"',
        'status.get("status") != "not-running"',
    ):
        assert phrase in runbook


def test_user_docs_cover_schema_artifacts_and_rpc() -> None:
    configuration = _read("docs/configuration.md")
    builds_models = _read("docs/builds-and-models.md")
    agent_rpc = _read("docs/agent-rpc.md")

    assert "command.build" in configuration
    assert "model_ref" in configuration
    assert "launch.required_hostname" in configuration
    assert "targets.yaml" in configuration
    assert "positional SSH arguments" in configuration
    assert "nightly and commit require uv" in builds_models
    assert "HF_TOKEN" in builds_models
    assert "controller passes only run_id" in agent_rpc
    assert "subscribe" in agent_rpc
    assert "VELA_AGENT_TOKEN" in agent_rpc
    assert "local_transport`: `socket` or `in_process`" in configuration
    assert "inprocess" not in configuration
    assert "Build removal has no --force override" in builds_models
    assert "Model removal --force only overrides config-pin protection" in builds_models
    assert "vela agent status --target <name>" in configuration
    assert "VELA_CONFIGS" in configuration
    assert "vela agent gen-token --install --target <name>" in configuration
    assert "write_agent_token" in agent_rpc
    assert "XDG_CONFIG_HOME" in configuration
    assert "XDG_DATA_HOME" in configuration
    assert "XDG_STATE_HOME" in configuration
    assert "XDG_RUNTIME_DIR" in configuration


def test_docs_cover_daemon_honesty_surfaces() -> None:
    configuration = _read("docs/configuration.md")
    agent_rpc = _read("docs/agent-rpc.md")

    # Socket-directory precedence (D5, bug-238) — the new socket-precedence prose.
    assert "VELA_AGENT_RUNTIME_DIR" in configuration
    # Stale-local-daemon version banner + the startup stderr log + the daemon-cwd
    # honesty (searched dirs) surfaced by the unknown-config error.
    assert "restart with: vela agent restart" in configuration
    assert "agent-start.err" in configuration
    assert "the directories it searched" in configuration
    # Handshake identity + the diagnostic unknown-config payload keys.
    assert "agent_revision" in agent_rpc
    assert "searched_dirs" in agent_rpc


def test_build_model_docs_cover_operational_cli_surfaces() -> None:
    text = _read("docs/builds-and-models.md")

    for phrase in (
        "vela build run",
        "vela build repair",
        "--copy",
        "vela model download tiny-llama --target blackbird --json",
        "vela model verify tiny-llama --target blackbird --deep",
    ):
        assert phrase in text


def test_docs_cover_launch_cache_check_and_registry_learning() -> None:
    text = _read("docs/builds-and-models.md")

    assert "require_cached_models" in text
    assert "--require-cached" in text
    assert "model-not-cached" in text
    assert "learns" in text or "re-scans" in text


def test_docs_cover_docker_hf_cache_default_mount() -> None:
    docker_runtime = _read("docs/docker-runtime.md")
    builds_models = _read("docs/builds-and-models.md")

    assert "mounts the agent HF cache by default" in docker_runtime
    assert "docker-no-hf-cache-mount" in docker_runtime
    assert "mounts the agent HF cache by default" in builds_models


def test_v15_docs_cover_native_docker_and_composer_surfaces() -> None:
    readme = _read("README.md")
    configuration = _read("docs/configuration.md")
    agent_rpc = _read("docs/agent-rpc.md")
    gpu_workflow = _read("docs/gpu-workflow.md")
    docker_runtime = _read("docs/docker-runtime.md")
    deployments = _read("docs/deployments.md")

    assert "New Deployment" in readme
    assert "vela deploy create" in readme
    assert "command.runtime" in configuration
    assert "command.docker" in configuration
    assert "compose_config" in agent_rpc
    assert "export_config" in agent_rpc
    assert "native `command.runtime: docker`" in docker_runtime
    assert "TUI is the primary deployment composer" in deployments
    assert "local Blackwell recipe" in deployments
    assert "Hugging Face metadata is advisory" in deployments
    assert "2026-06-06-p620-blackbird-native-docker-fp8" in gpu_workflow
    assert "2026-06-06-p620-blackbird-native-docker-bf16" in gpu_workflow
    assert "VELA_SMOKE_RUN_ID" in gpu_workflow
    assert "REAL_MODEL_RECOVERY_OK" in gpu_workflow
    assert "mode=ssh-reconnect" in gpu_workflow
    assert "run_id=<run_id>" in gpu_workflow


def test_docs_cover_offline_pins_and_disk_prechecks() -> None:
    builds_models = _read("docs/builds-and-models.md")
    docker_runtime = _read("docs/docker-runtime.md")
    configuration = _read("docs/configuration.md")

    # --offline / validated:false and the --commit-sha gating guarantee (5.8/M5).
    assert "--offline" in builds_models
    assert "validated: false" in builds_models
    assert "--offline" in configuration
    assert "validated: false" in configuration
    # Disk-headroom prechecks (5.9) — the resolved cache dir needs size + 10%.
    assert "insufficient-disk" in builds_models
    assert "disk-headroom" in builds_models
    assert "disk-headroom" in docker_runtime
    assert "disk-headroom" in configuration


def test_docs_cover_docker_pull_timeout_and_progress() -> None:
    docker_runtime = _read("docs/docker-runtime.md")
    configuration = _read("docs/configuration.md")

    assert "VELA_DOCKER_PULL_TIMEOUT_SECONDS" in docker_runtime
    assert "VELA_DOCKER_PULL_TIMEOUT_SECONDS" in configuration
    assert "image-pull-timeout" in docker_runtime


def test_lab_topology_docs_use_current_repo_and_venv_paths() -> None:
    for path in (
        "README.md",
        "docs/configuration.md",
        "docs/docker-runtime.md",
        "docs/gpu-workflow.md",
    ):
        text = _read(path)
        assert "/home/bgconley/repos/vela" not in text
        assert "/home/bgconley/venvs/vela" not in text
        assert "/home/bgconley/repos/current-vela" not in text
        assert "/home/bgconley/venvs/current-vela" not in text
        assert "/Users/brennanconley/vibecode/infx/ubuntu24_ed25519" not in text


def test_blackwell_docs_treat_local_recipes_as_runtime_truth() -> None:
    docker_runtime = _read("docs/docker-runtime.md")
    configuration = _read("docs/configuration.md")

    assert "local Blackwell recipe" in docker_runtime
    assert "Hugging Face metadata is advisory" in docker_runtime
    assert "sm_120" in docker_runtime
    assert "CUTLASS" in docker_runtime
    assert "FlashInfer" in docker_runtime
    assert "intentionally emit both `--ipc=host` and `--shm-size 32g`" in docker_runtime
    assert "vela deploy from-wrapper" in docker_runtime
    assert "does not infer vLLM image" in docker_runtime
    assert "local deployment scripts" in configuration


def test_docker_examples_doc_matches_native_docker_cutover() -> None:
    text = _read("docs/specs/vela-docker-runtime-examples-v1.md")

    assert "runtime has shipped" in text
    assert "configs/qwen36-27b-fp8-kvfp8-rp6000-blackbird.yaml" in text
    assert "wrappers are retained as reference" in text
    assert "Do not drop these into `configs/` yet" not in text
    assert "delete the wrapper scripts" not in text


def test_docs_cover_phase7_cli_surfaces() -> None:
    configuration = _read("docs/configuration.md")
    agent_rpc = _read("docs/agent-rpc.md")

    # 7.3: default-target resolution and the command that persists it.
    assert "VELA_TARGET" in configuration
    assert "vela targets use" in configuration
    # 7.4: one canonical command per operation.
    assert "canonical" in configuration
    # 7.2: the run-lifecycle CLI trio.
    assert "vela runs list" in agent_rpc
    assert "vela stop" in agent_rpc
    assert "vela logs" in agent_rpc
