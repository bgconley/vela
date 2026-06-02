from __future__ import annotations

from vllm_loader.engine.phases import ErrorKind, Phase, PhaseFSM
from vllm_loader.engine.profile import bundled_profile


def walk(lines: list[str]) -> PhaseFSM:
    fsm = PhaseFSM(bundled_profile("current"))
    for line in lines:
        fsm.feed_line(line)
    return fsm


def test_success_fixture_produces_expected_phase_sequence() -> None:
    fsm = walk(
        [
            "INFO Initializing a V1 LLM engine",
            "INFO Fetching 4 files",
            "INFO Downloading model file",
            "INFO Starting to load model",
            "INFO GPU KV cache size: 123",
            "INFO Capturing CUDA graph shapes",
            "INFO Uvicorn running on http://127.0.0.1:8000",
        ]
    )
    fsm.health_ready(["llama"])

    assert fsm.history == [
        Phase.STARTING,
        Phase.RESOLVING_MODEL,
        Phase.DOWNLOADING_MODEL,
        Phase.LOADING_WEIGHTS,
        Phase.PROFILING_KV,
        Phase.CAPTURING_GRAPHS,
        Phase.SERVER_STARTING,
        Phase.READY,
    ]


def test_error_classification_patterns() -> None:
    cases = {
        "CUDA out of memory": ErrorKind.OOM,
        "address already in use [Errno 98]": ErrorKind.PORT_IN_USE,
        "RepositoryNotFoundError model missing": ErrorKind.MODEL_NOT_FOUND,
        "world_size must be divisible by tensor_parallel_size": ErrorKind.TP_MISMATCH,
        "GatedRepoError Cannot access gated repo": ErrorKind.HF_AUTH,
    }

    for line, kind in cases.items():
        fsm = walk([line])
        assert fsm.error_kind == kind
        assert fsm.phase is Phase.ERROR


def test_ready_comes_from_health_not_log_line_alone() -> None:
    fsm = walk(["INFO Uvicorn running on http://127.0.0.1:8000"])

    assert fsm.phase is Phase.SERVER_STARTING

    fsm.health_ready(["llama"])
    assert fsm.phase is Phase.READY


def test_ready_degraded_ready_recovery() -> None:
    fsm = PhaseFSM(bundled_profile("current"))
    fsm.health_ready(["llama"])
    fsm.health_failed("500")
    fsm.health_ready(["llama"])

    assert fsm.history == [Phase.READY, Phase.DEGRADED, Phase.READY]


def test_nonzero_exit_before_ready_is_crashed_unless_better_error() -> None:
    fsm = PhaseFSM(bundled_profile("current"))
    fsm.process_exited(1)

    assert fsm.phase is Phase.ERROR
    assert fsm.error_kind is ErrorKind.CRASHED

    better = walk(["CUDA out of memory"])
    better.process_exited(1)
    assert better.error_kind is ErrorKind.OOM


def test_generic_crash_uses_recent_log_excerpt() -> None:
    fsm = walk(["INFO starting", "ERROR synthetic loader abort before ready"])
    fsm.process_exited(7)

    assert fsm.phase is Phase.ERROR
    assert fsm.error_kind is ErrorKind.CRASHED
    assert fsm.error_excerpt == "ERROR synthetic loader abort before ready"


def test_readiness_timeout_classifies_timed_out() -> None:
    fsm = PhaseFSM(bundled_profile("current"))
    fsm.timeout()

    assert fsm.phase is Phase.ERROR
    assert fsm.error_kind is ErrorKind.TIMED_OUT


def test_health_error_kind_transitions_to_named_error() -> None:
    fsm = PhaseFSM(bundled_profile("current"))
    fsm.health_error(ErrorKind.HF_AUTH, "Bearer token mismatch")

    assert fsm.phase is Phase.ERROR
    assert fsm.error_kind is ErrorKind.HF_AUTH
    assert fsm.error_excerpt == "Bearer token mismatch"
