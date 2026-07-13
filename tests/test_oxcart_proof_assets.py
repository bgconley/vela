from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from vela.config.loader import ValidConfig, load_config_file

PROFILE_PATH = Path("configs/oxcart-qwen36-27b-fp8-mtp-vl.yaml")
PROFILE_NAME = "oxcart-qwen36-27b-fp8-mtp-vl"
MODEL_REVISION = "e89b16ebf1988b3d6befa7de50abc2d76f26eb09"
IMAGE = (
    "vllm/vllm-openai@sha256:"
    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
)
CONTAINER_NAME = "vela-oxcart-qwen36-27b-fp8-mtp-vl"
WIZARD_MODEL_ENTRY_ID = "01J123456789ABCDEFGHJKMNPQ"
OWNERSHIP_LABELS = {
    "ai.vela.managed": "true",
    "ai.vela.profile": PROFILE_NAME,
}


def _load_script(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _profile_payload() -> dict[str, object]:
    payload = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _valid_backend_log() -> str:
    return "\n".join(
        [
            "INFO Selected CutlassFp8BlockScaledMMKernel for Fp8LinearMethod",
            "INFO Using AttentionBackendEnum.FLASHINFER backend",
            "INFO Qwen3_5MTP speculative decoding enabled",
        ]
    )


def _artifact_identity(
    *,
    model_ref: str = WIZARD_MODEL_ENTRY_ID,
    model_entry_id: str = WIZARD_MODEL_ENTRY_ID,
    model_repo_id: str = "Qwen/Qwen3.6-27B-FP8",
    model_commit_sha: str = MODEL_REVISION,
) -> dict[str, object]:
    return {
        "config_name": PROFILE_NAME,
        "model_ref": model_ref,
        "model_entry_id": model_entry_id,
        "model_repo_id": model_repo_id,
        "model_revision": MODEL_REVISION,
        "model_commit_sha": model_commit_sha,
        "served_model_names": ["qwen36-27b-fp8-oxcart"],
        "runtime": "docker",
        "docker_container_name": CONTAINER_NAME,
        "docker_container_id": "container-actual",
        "docker_image_digest": IMAGE.split("@", 1)[1],
    }


def test_checked_in_oxcart_profile_is_exact_and_pin_gated() -> None:
    loaded = load_config_file(PROFILE_PATH)

    assert isinstance(loaded, ValidConfig), getattr(loaded, "errors", None)
    config = loaded.config
    assert config.name == PROFILE_NAME
    assert config.target == "local"
    assert config.model == "Qwen/Qwen3.6-27B-FP8"
    assert config.revision == MODEL_REVISION
    assert config.model_ref == "Qwen/Qwen3.6-27B-FP8"
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 18004
    assert config.server.exposure.value == "local"
    assert config.launch.require_cached_models is True
    assert config.launch.required_hostname == "oxcart"

    docker = config.command.docker
    assert docker is not None
    assert docker.image == IMAGE
    assert docker.container_name == CONTAINER_NAME
    assert docker.pull == "never"
    assert docker.auto_remove is True
    # Name collisions fail closed. The only authorized cleanup path is the
    # separate guard, which rechecks both ownership labels and container ID.
    assert docker.evict == []
    assert docker.extra_run_args == [
        "--label",
        "ai.vela.managed=true",
        "--label",
        f"ai.vela.profile={PROFILE_NAME}",
    ]
    assert "PIN_REQUIRED" in PROFILE_PATH.read_text(encoding="utf-8")


def test_oxcart_backend_evidence_accepts_exact_profile() -> None:
    module = _load_script(
        Path("scripts/backend_evidence_check.py"),
        "backend_evidence_check_oxcart_accept",
    )

    result = module.validate_backend_evidence(
        PROFILE_NAME,
        _profile_payload(),
        _valid_backend_log(),
    )

    assert result == {
        "checked": True,
        "config_name": PROFILE_NAME,
        "required": {"cutlass_fp8": True, "flashinfer_attention": True},
        "forbidden": {"marlin_fallback": False},
    }
    json.dumps(result)


def test_oxcart_backend_evidence_accepts_scrubbed_run_artifact() -> None:
    module = _load_script(
        Path("scripts/backend_evidence_check.py"),
        "backend_evidence_check_oxcart_scrubbed_artifact",
    )
    artifact = _profile_payload()
    artifact["server"]["api_key"] = None

    result = module.validate_backend_evidence(PROFILE_NAME, artifact, _valid_backend_log())

    assert result["checked"] is True


def test_oxcart_backend_evidence_accepts_repo_alias_with_matching_identity() -> None:
    module = _load_script(
        Path("scripts/backend_evidence_check.py"),
        "backend_evidence_check_oxcart_repo_alias_identity",
    )

    result = module.validate_backend_evidence(
        PROFILE_NAME,
        _profile_payload(),
        _valid_backend_log(),
        identity=_artifact_identity(
            model_ref="Qwen/Qwen3.6-27B-FP8",
            model_entry_id=WIZARD_MODEL_ENTRY_ID,
        ),
    )

    assert result["checked"] is True


def test_oxcart_backend_evidence_accepts_wizard_concrete_model_ref_with_identity() -> (
    None
):
    module = _load_script(
        Path("scripts/backend_evidence_check.py"),
        "backend_evidence_check_oxcart_wizard_identity",
    )
    artifact = _profile_payload()
    artifact["model_ref"] = WIZARD_MODEL_ENTRY_ID

    result = module.validate_backend_evidence(
        PROFILE_NAME,
        artifact,
        _valid_backend_log(),
        identity=_artifact_identity(),
    )

    assert result["checked"] is True


@pytest.mark.parametrize(
    ("config_ref", "identity", "expected_error"),
    [
        (
            WIZARD_MODEL_ENTRY_ID,
            None,
            "model_ref must use the stable Oxcart repo-id alias",
        ),
        (
            "friendly-display-alias",
            _artifact_identity(
                model_ref="friendly-display-alias",
                model_entry_id="friendly-display-alias",
            ),
            "model_ref must use the stable Oxcart repo-id alias",
        ),
        (
            WIZARD_MODEL_ENTRY_ID,
            _artifact_identity(model_entry_id="01J00000000000000000000000"),
            "model_ref must use the stable Oxcart repo-id alias",
        ),
        (
            WIZARD_MODEL_ENTRY_ID,
            _artifact_identity(model_repo_id="attacker/wrong-model"),
            "artifact model_repo_id must be Qwen/Qwen3.6-27B-FP8",
        ),
        (
            WIZARD_MODEL_ENTRY_ID,
            _artifact_identity(model_commit_sha="deadbeef"),
            "artifact model_commit_sha must be the immutable Oxcart model commit",
        ),
        (
            "Qwen/Qwen3.6-27B-FP8",
            _artifact_identity(
                model_ref="Qwen/Qwen3.6-27B-FP8",
                model_repo_id="attacker/wrong-model",
            ),
            "artifact model_repo_id must be Qwen/Qwen3.6-27B-FP8",
        ),
        (
            "Qwen/Qwen3.6-27B-FP8",
            _artifact_identity(
                model_ref="Qwen/Qwen3.6-27B-FP8",
                model_commit_sha="deadbeef",
            ),
            "artifact model_commit_sha must be the immutable Oxcart model commit",
        ),
    ],
)
def test_oxcart_backend_evidence_rejects_unproven_wizard_model_identity(
    config_ref: str,
    identity: dict[str, object] | None,
    expected_error: str,
) -> None:
    module = _load_script(
        Path("scripts/backend_evidence_check.py"),
        f"backend_evidence_check_oxcart_identity_{abs(hash(expected_error + config_ref))}",
    )
    artifact = _profile_payload()
    artifact["model_ref"] = config_ref

    with pytest.raises(module.BackendEvidenceError, match=expected_error):
        module.validate_backend_evidence(
            PROFILE_NAME,
            artifact,
            _valid_backend_log(),
            identity=identity,
        )


@pytest.mark.parametrize(
    ("log_text", "expected_error"),
    [
        (
            "INFO Using AttentionBackendEnum.FLASHINFER backend",
            "missing required backend evidence: cutlass_fp8",
        ),
        (
            "INFO Selected CutlassFp8BlockScaledMMKernel",
            "missing required backend evidence: flashinfer_attention",
        ),
        (
            _valid_backend_log() + "\nINFO Selected MARLIN fallback backend",
            "forbidden backend evidence detected: marlin_fallback",
        ),
    ],
)
def test_oxcart_backend_evidence_requires_proven_backends(
    log_text: str,
    expected_error: str,
) -> None:
    module = _load_script(
        Path("scripts/backend_evidence_check.py"),
        f"backend_evidence_check_oxcart_log_{abs(hash(expected_error))}",
    )

    with pytest.raises(module.BackendEvidenceError, match=expected_error):
        module.validate_backend_evidence(
            PROFILE_NAME,
                _profile_payload(),
            log_text,
        )


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (
            lambda config: config["command"]["docker"].update(
                {"image": "vllm/vllm-openai:latest"}
            ),
            "command.docker.image does not match pinned Oxcart image",
        ),
        (
            lambda config: config.update({"revision": "main"}),
            "revision must be the immutable Oxcart model commit",
        ),
        (
            lambda config: config.update(
                {"model_ref": "some/display-name"}
            ),
            "model_ref must use the stable Oxcart repo-id alias",
        ),
        (
            lambda config: config["server"].update({"port": 18002}),
            "server must be local-only on 127.0.0.1:18004",
        ),
        (
            lambda config: config["launch"].update({"required_hostname": "blackbird"}),
            "launch.required_hostname must be oxcart",
        ),
        (
            lambda config: config["command"]["docker"].update(
                {"evict": [CONTAINER_NAME]}
            ),
            "command.docker.evict must be omitted",
        ),
        (
            lambda config: config["command"]["docker"].update({"auto_remove": False}),
            "command.docker.auto_remove must be true",
        ),
        (
            lambda config: config["command"]["docker"].update({"extra_run_args": []}),
            "command.docker.extra_run_args must contain only the two ownership labels",
        ),
        (
            lambda config: config["extra_args"].append("--language-model-only"),
            "extra_args must omit --language-model-only",
        ),
    ],
)
def test_oxcart_backend_evidence_rejects_config_drift(mutator, expected_error: str) -> None:
    module = _load_script(
        Path("scripts/backend_evidence_check.py"),
        f"backend_evidence_check_oxcart_shape_{abs(hash(expected_error))}",
    )
    config = copy.deepcopy(_profile_payload())
    mutator(config)

    with pytest.raises(module.BackendEvidenceError, match=expected_error):
        module.validate_backend_evidence(PROFILE_NAME, config, _valid_backend_log())


def test_oxcart_backend_evidence_fails_closed_for_unregistered_profile() -> None:
    module = _load_script(
        Path("scripts/backend_evidence_check.py"),
        "backend_evidence_check_oxcart_fail_closed",
    )
    config = _profile_payload()
    config["name"] = f"{PROFILE_NAME}-renamed"

    with pytest.raises(
        module.BackendEvidenceError,
        match="unregistered backend evidence rule for Oxcart config",
    ):
        module.validate_backend_evidence(
            f"{PROFILE_NAME}-renamed",
            config,
            _valid_backend_log(),
        )


def _guard_module(module_name: str) -> ModuleType:
    return _load_script(Path("scripts/oxcart_live_guard.py"), module_name)


def test_oxcart_guard_uses_the_hub_below_the_profile_hf_home() -> None:
    module = _guard_module("oxcart_live_guard_cache_path")

    assert module.MODEL_CACHE_REPO == Path(
        "/tank/ai/models/qwen36-27b-fp8/hf-cache/hub/"
        "models--Qwen--Qwen3.6-27B-FP8"
    )


def _healthy_guard_state(module: ModuleType):
    return module.GuardState(
        hostname="oxcart",
        listening_ports=frozenset(),
        image_repo_digests=(IMAGE,),
        # refs/main is mutable provenance only; the guard validates the exact
        # MODEL_REVISION snapshot represented by cache_files.
        cache_main_ref="newer-main-commit",
        cache_files=frozenset({"config.json", "model.safetensors.index.json"}),
        gpu_memory_used_mib=(2,),
        gpu_compute_processes=(),
        containers=(
            module.ContainerState(
                container_id="unrelated-id",
                name="litellm-proxy",
                image="litellm:stable",
                status="running",
                running=True,
                labels={},
            ),
        ),
    )


def test_oxcart_guard_preflight_is_read_only_and_snapshots_unrelated_containers() -> None:
    module = _guard_module("oxcart_live_guard_preflight")
    state = _healthy_guard_state(module)

    report = module.validate_preflight(state)

    assert report["ok"] is True
    assert report["failures"] == []
    assert report["snapshot"] == {
        "schema": "vela.oxcart-live-baseline.v1",
        "hostname": "oxcart",
        "cache_main_ref": "newer-main-commit",
        "model_snapshot_revision": MODEL_REVISION,
        "gpu_memory_used_mib": [2],
        "unrelated_containers": {
            "litellm-proxy": {
                "container_id": "unrelated-id",
                "image": "litellm:stable",
                "status": "running",
                "running": True,
                "started_at": "",
                "restart_count": 0,
                "exit_code": 0,
            }
        },
    }


@pytest.mark.parametrize(
    ("field", "value", "check"),
    [
        ("hostname", "blackbird", "hostname"),
        ("listening_ports", frozenset({18004}), "port"),
        ("image_repo_digests", (), "image"),
        ("cache_files", frozenset(), "cache"),
        ("gpu_memory_used_mib", (2048,), "gpu-baseline"),
        ("gpu_compute_processes", ("pid=123 vllm",), "gpu-processes"),
    ],
)
def test_oxcart_guard_preflight_fails_closed_on_host_resource_drift(
    field: str,
    value: object,
    check: str,
) -> None:
    module = _guard_module(f"oxcart_live_guard_preflight_{field}")
    state = _healthy_guard_state(module)
    state = module.dataclasses.replace(state, **{field: value})

    report = module.validate_preflight(state)

    assert report["ok"] is False
    assert check in {failure["check"] for failure in report["failures"]}


def test_oxcart_guard_rejects_validation_container_without_both_labels() -> None:
    module = _guard_module("oxcart_live_guard_labels")
    state = _healthy_guard_state(module)
    state = module.dataclasses.replace(
        state,
        containers=state.containers
        + (
            module.ContainerState(
                container_id="foreign-id",
                name=CONTAINER_NAME,
                image=IMAGE,
                status="exited",
                running=False,
                labels={"ai.vela.managed": "true"},
            ),
        ),
    )

    report = module.validate_preflight(state)

    assert report["ok"] is False
    assert "container-ownership" in {
        failure["check"] for failure in report["failures"]
    }


def test_oxcart_guard_postflight_detects_residue_and_unrelated_container_change() -> None:
    module = _guard_module("oxcart_live_guard_postflight")
    baseline_state = _healthy_guard_state(module)
    baseline = module.validate_preflight(baseline_state)["snapshot"]
    current = module.dataclasses.replace(
        baseline_state,
        listening_ports=frozenset({18004}),
        gpu_memory_used_mib=(4096,),
        containers=(
            module.ContainerState(
                container_id="replacement-id",
                name="litellm-proxy",
                image="litellm:stable",
                status="running",
                running=True,
                labels={},
            ),
            module.ContainerState(
                container_id="owned-id",
                name=CONTAINER_NAME,
                image=IMAGE,
                status="exited",
                running=False,
                labels=OWNERSHIP_LABELS,
            ),
        ),
    )

    report = module.validate_postflight(current, baseline)

    assert report["ok"] is False
    assert {failure["check"] for failure in report["failures"]} == {
        "port-residue",
        "gpu-residue",
        "container-residue",
        "unrelated-containers",
    }


class _CleanupProbe:
    def __init__(self, module: ModuleType, labels: dict[str, str], *, running: bool) -> None:
        self._module = module
        self._container = module.ContainerState(
            container_id="validation-id",
            name=CONTAINER_NAME,
            image=IMAGE,
            status="running" if running else "exited",
            running=running,
            labels=labels,
        )
        self.mutations: list[tuple[object, ...]] = []

    def hostname(self) -> str:
        return "oxcart"

    def validation_container(self):
        return self._container

    def container_by_id(self, container_id: str):
        assert container_id == "validation-id"
        if self._container is None:
            return None
        return self._container

    def stop_container(self, container_id: str, timeout_seconds: int) -> None:
        self.mutations.append(("stop", container_id, timeout_seconds))
        self._container = self._module.dataclasses.replace(
            self._container,
            status="exited",
            running=False,
        )

    def remove_container(self, container_id: str) -> None:
        self.mutations.append(("remove", container_id))
        self._container = None


def test_oxcart_guard_cleanup_refuses_foreign_container_without_mutation() -> None:
    module = _guard_module("oxcart_live_guard_cleanup_refuse")
    probe = _CleanupProbe(module, {"ai.vela.managed": "true"}, running=False)

    with pytest.raises(module.GuardError, match="both ownership labels"):
        module.cleanup_validation_container(probe)

    assert probe.mutations == []


def test_oxcart_guard_cleanup_rechecks_identity_then_stops_and_removes_owned_container() -> (
    None
):
    module = _guard_module("oxcart_live_guard_cleanup_owned")
    probe = _CleanupProbe(module, OWNERSHIP_LABELS, running=True)

    result = module.cleanup_validation_container(probe)

    assert result == {
        "cleaned": True,
        "container_id": "validation-id",
        "stopped": True,
        "removed": True,
    }
    assert probe.mutations == [
        ("stop", "validation-id", 90),
        ("remove", "validation-id"),
    ]
