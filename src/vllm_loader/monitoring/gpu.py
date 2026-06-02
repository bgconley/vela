from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class GpuSample:
    visible_index: int
    uuid: str
    name: str
    memory_used_mb: int
    memory_total_mb: int
    utilization_percent: int | None = None
    temperature_c: int | None = None
    power_w: int | None = None
    mig_instance_id: str | None = None


@dataclass(frozen=True)
class CudaVisibleDevices:
    raw: str
    numeric: list[int]
    uuids: list[str]


@dataclass(frozen=True)
class GpuPollResult:
    samples: list[GpuSample]
    note: str = ""
    unavailable: bool = False


def sample_gpus(cuda_visible_devices: str | None = None) -> GpuPollResult:
    try:
        samples = _sample_nvml()
    except Exception:
        try:
            samples = _sample_nvidia_smi()
        except Exception:
            return GpuPollResult([], note="GPU stats unavailable", unavailable=True)
    return apply_cuda_visible_devices(
        samples, cuda_visible_devices or os.environ.get("CUDA_VISIBLE_DEVICES")
    )


def _sample_nvml() -> list[GpuSample]:
    import pynvml  # type: ignore[import-not-found]

    pynvml.nvmlInit()
    samples: list[GpuSample] = []
    for index in range(pynvml.nvmlDeviceGetCount()):
        handle = pynvml.nvmlDeviceGetHandleByIndex(index)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        power = None
        try:
            power = int(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000)
        except Exception:
            pass
        samples.append(
            GpuSample(
                visible_index=index,
                uuid=_decode(pynvml.nvmlDeviceGetUUID(handle)),
                name=_decode(pynvml.nvmlDeviceGetName(handle)),
                memory_used_mb=int(mem.used / 1024 / 1024),
                memory_total_mb=int(mem.total / 1024 / 1024),
                utilization_percent=int(util.gpu),
                temperature_c=int(
                    pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                ),
                power_w=power,
                mig_instance_id=_mig_instance_id(pynvml, handle),
            )
        )
    return samples


def _sample_nvidia_smi() -> list[GpuSample]:
    fields = "index,uuid,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"
    proc = subprocess.run(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "nvidia-smi failed")
    return parse_nvidia_smi_csv(proc.stdout)


def parse_nvidia_smi_csv(text: str) -> list[GpuSample]:
    samples: list[GpuSample] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 8:
            continue
        samples.append(
            GpuSample(
                visible_index=int(parts[0]),
                uuid=parts[1],
                name=parts[2],
                memory_used_mb=_int_or_zero(parts[3]),
                memory_total_mb=_int_or_zero(parts[4]),
                utilization_percent=_int_or_none(parts[5]),
                temperature_c=_int_or_none(parts[6]),
                power_w=_int_or_none(parts[7].split(".")[0]),
            )
        )
    return samples


def parse_cuda_visible_devices(value: str | None) -> CudaVisibleDevices:
    if not value:
        return CudaVisibleDevices(raw="", numeric=[], uuids=[])
    numeric: list[int] = []
    uuids: list[str] = []
    for part in [item.strip() for item in value.split(",") if item.strip()]:
        if part.isdigit():
            numeric.append(int(part))
        elif part.startswith("GPU-") or part.startswith("MIG-"):
            uuids.append(part)
    return CudaVisibleDevices(raw=value, numeric=numeric, uuids=uuids)


def apply_cuda_visible_devices(samples: list[GpuSample], value: str | None) -> GpuPollResult:
    parsed = parse_cuda_visible_devices(value)
    if not parsed.raw:
        return GpuPollResult(samples)
    selected: list[GpuSample] = []
    if parsed.numeric:
        for visible_index, physical_index in enumerate(parsed.numeric):
            if 0 <= physical_index < len(samples):
                selected.append(replace(samples[physical_index], visible_index=visible_index))
    elif parsed.uuids:
        for visible_index, uuid in enumerate(parsed.uuids):
            for sample in samples:
                if sample.uuid == uuid:
                    selected.append(replace(sample, visible_index=visible_index))
                    break
    if selected:
        return GpuPollResult(selected)
    return GpuPollResult(
        samples, note="CUDA_VISIBLE_DEVICES mapping ambiguous; showing all NVML-visible GPUs"
    )


def _decode(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _mig_instance_id(pynvml, handle) -> str | None:
    try:
        gpu_instance = pynvml.nvmlDeviceGetGpuInstanceId(handle)
        compute_instance = pynvml.nvmlDeviceGetComputeInstanceId(handle)
    except Exception:
        return None
    return f"GI {gpu_instance} / CI {compute_instance}"


def _int_or_zero(value: str) -> int:
    return _int_or_none(value) or 0


def _int_or_none(value: str) -> int | None:
    try:
        return int(float(value.strip()))
    except Exception:
        return None
