from __future__ import annotations

import sys
from types import SimpleNamespace

from vllm_loader.monitoring.gpu import (
    GpuSample,
    apply_cuda_visible_devices,
    parse_cuda_visible_devices,
    parse_nvidia_smi_csv,
)


def test_nvml_unavailable_path_returns_unavailable(monkeypatch) -> None:
    from vllm_loader.monitoring import gpu

    monkeypatch.setattr(gpu, "_sample_nvml", lambda: (_ for _ in ()).throw(RuntimeError("no nvml")))
    monkeypatch.setattr(
        gpu, "_sample_nvidia_smi", lambda: (_ for _ in ()).throw(RuntimeError("no smi"))
    )

    result = gpu.sample_gpus()

    assert result.unavailable
    assert "GPU stats unavailable" in result.note


def test_nvidia_smi_fallback_parsing() -> None:
    parsed = parse_nvidia_smi_csv(
        "0, GPU-aaa, NVIDIA A100, 1024, 81920, 55, 42, 110\n"
        "1, GPU-bbb, NVIDIA A100, 2048, 81920, 65, 43, 120\n"
    )

    assert parsed[0].visible_index == 0
    assert parsed[0].uuid == "GPU-aaa"
    assert parsed[0].memory_used_mb == 1024
    assert parsed[1].utilization_percent == 65


def test_nvml_sampling_includes_mig_instance_identity(monkeypatch) -> None:
    from vllm_loader.monitoring import gpu

    fake_pynvml = SimpleNamespace(
        NVML_TEMPERATURE_GPU=0,
        nvmlInit=lambda: None,
        nvmlDeviceGetCount=lambda: 1,
        nvmlDeviceGetHandleByIndex=lambda _index: object(),
        nvmlDeviceGetMemoryInfo=lambda _handle: SimpleNamespace(
            used=1024 * 1024 * 1024,
            total=80 * 1024 * 1024 * 1024,
        ),
        nvmlDeviceGetUtilizationRates=lambda _handle: SimpleNamespace(gpu=77),
        nvmlDeviceGetPowerUsage=lambda _handle: 123000,
        nvmlDeviceGetTemperature=lambda _handle, _sensor: 45,
        nvmlDeviceGetUUID=lambda _handle: b"GPU-mig-parent",
        nvmlDeviceGetName=lambda _handle: b"NVIDIA A100",
        nvmlDeviceGetGpuInstanceId=lambda _handle: 3,
        nvmlDeviceGetComputeInstanceId=lambda _handle: 7,
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake_pynvml)

    [sample] = gpu._sample_nvml()

    assert sample.uuid == "GPU-mig-parent"
    assert sample.mig_instance_id == "GI 3 / CI 7"


def test_cuda_visible_devices_numeric_and_uuid_handling() -> None:
    samples = [
        GpuSample(visible_index=0, uuid="GPU-a", name="A", memory_used_mb=1, memory_total_mb=2),
        GpuSample(visible_index=1, uuid="GPU-b", name="B", memory_used_mb=1, memory_total_mb=2),
    ]

    assert parse_cuda_visible_devices("0,1").numeric == [0, 1]
    assert parse_cuda_visible_devices("GPU-b").uuids == ["GPU-b"]
    assert [s.uuid for s in apply_cuda_visible_devices(samples, "1").samples] == ["GPU-b"]
    assert [s.uuid for s in apply_cuda_visible_devices(samples, "GPU-a").samples] == ["GPU-a"]


def test_ambiguous_mapping_produces_note_not_crash() -> None:
    samples = [
        GpuSample(visible_index=0, uuid="GPU-a", name="A", memory_used_mb=1, memory_total_mb=2)
    ]

    result = apply_cuda_visible_devices(samples, "not-a-gpu")

    assert result.samples == samples
    assert "ambiguous" in result.note.lower()
