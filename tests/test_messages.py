from __future__ import annotations

from textual.message import Message

import vllm_loader.messages as messages
from vllm_loader.engine.log_sink import LogRecord
from vllm_loader.engine.phases import ErrorKind, Phase
from vllm_loader.monitoring.gpu import GpuPollResult


def test_canonical_event_taxonomy_uses_textual_messages() -> None:
    for name in (
        "LogLineCommitted",
        "ProgressUpdated",
        "PhaseChanged",
        "ServerReady",
        "HealthChanged",
        "ProcessExited",
        "EngineError",
        "GpuStatsUpdated",
        "GpuStatsUnavailable",
    ):
        message_type = getattr(messages, name)
        assert issubclass(message_type, Message)


def test_message_payloads_match_engine_monitoring_contract() -> None:
    assert messages.LogLineCommitted("INFO ready", "INFO").level == "INFO"
    assert messages.ProgressUpdated("Downloading 20% 1/5").text == "Downloading 20% 1/5"
    assert messages.PhaseChanged(Phase.READY).phase is Phase.READY
    assert messages.ServerReady(["model"]).models == ["model"]
    assert (
        messages.HealthChanged(ready=False, detail="health returned 503").detail
        == "health returned 503"
    )
    assert messages.ProcessExited(7).returncode == 7
    assert messages.EngineError(ErrorKind.OOM, "CUDA out of memory").kind is ErrorKind.OOM
    assert messages.GpuStatsUpdated(GpuPollResult([])).result.samples == []
    assert messages.GpuStatsUnavailable("no nvml").detail == "no nvml"


def test_log_record_conversion_uses_progress_updated_for_transient_records() -> None:
    committed = messages.from_log_record(LogRecord("committed", "INFO server", "INFO"))
    transient = messages.from_log_record(LogRecord("transient", "Loading 33% 1/3", None))

    assert isinstance(committed, messages.LogLineCommitted)
    assert committed.text == "INFO server"
    assert isinstance(transient, messages.ProgressUpdated)
    assert transient.text == "Loading 33% 1/3"
