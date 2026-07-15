"""Regenerate the legacy placeholder-only overview screenshots.

Renders the TUI headlessly (Textual run_test pilot) against fabricated,
placeholder-only state — no real hosts, users, or paths — and saves SVGs.
Convert to PNG afterwards, e.g. on macOS:

    python3 scripts/readme_screenshots.py docs/img
    qlmanage -t -s 1600 -o docs/img docs/img/*.svg

Run from the repo root.

The screenshot-led user tutorial uses live, checksummed workflow captures
published by ``scripts/sync_docs_screenshots.py``. This helper remains useful
for neutral architecture/empty-state captures and must not teach mutable runtime
identity.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_tmp_state = tempfile.mkdtemp(prefix="vela-shots-")
os.environ["XDG_CONFIG_HOME"] = str(Path(_tmp_state) / "config")
os.environ["XDG_STATE_HOME"] = str(Path(_tmp_state) / "state")

from vela.tui.app import VelaApp  # noqa: E402

QWEN_CONFIG = """\
name: qwen3-8b-bf16
target: gpu-node
model: Qwen/Qwen3-8B
revision: main
command:
  entrypoint: serve
  runtime: docker
  docker:
    image: vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046
engine:
  tensor_parallel_size: 1
  gpu_memory_utilization: 0.95
  max_model_len: 32768
  dtype: bfloat16
server:
  host: 0.0.0.0
  port: 18002
  exposure: lan
launch:
  mode: detached
  ready_timeout_seconds: 900
env:
  CUDA_VISIBLE_DEVICES: "0"
"""

TINY_CONFIG = """\
name: tiny-llama-smoke
target: gpu-node
model: hf-internal-testing/tiny-random-LlamaForCausalLM
command:
  entrypoint: serve
  runtime: process
engine:
  tensor_parallel_size: 1
server:
  host: 127.0.0.1
  port: 8000
launch:
  mode: detached
"""


def _config_payload(path: Path, name: str, model: str) -> dict:
    import yaml

    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        "path": str(path),
        "name": name,
        "model": model,
        "target": "gpu-node",
        "warnings": [],
        "config": config,
    }


class ScreenshotClient:
    """Minimal target-client fake serving placeholder data."""

    def __init__(self, configs_dir: Path) -> None:
        self.configs_dir = configs_dir
        self.connected = False
        self._gpu_requested = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def subscribe(self, keys, resume_from="live"):
        return self._agent_events()

    async def _agent_events(self):
        await self._gpu_requested.wait()
        yield {
            "event": "gpu",
            "sub_id": "gpu-panel",
            "samples": [
                {
                    "visible_index": 0,
                    "uuid": "GPU-00000000-1111-2222-3333-444444444444",
                    "name": "NVIDIA Blackwell sm_120",
                    "memory_used_mb": 24576,
                    "memory_total_mb": 97887,
                    "utilization_percent": 63,
                    "temperature_c": 61,
                    "power_w": 285,
                }
            ],
        }
        await asyncio.Event().wait()

    async def call(self, method: str, params):
        if method == "gpu":
            self._gpu_requested.set()
            return {"ok": True}
        if method == "list_configs":
            return {
                "valid": [
                    _config_payload(
                        self.configs_dir / "qwen3-8b-bf16.yaml",
                        "qwen3-8b-bf16",
                        "Qwen/Qwen3-8B",
                    ),
                    _config_payload(
                        self.configs_dir / "tiny-llama-smoke.yaml",
                        "tiny-llama-smoke",
                        "hf-internal-testing/tiny-random-LlamaForCausalLM",
                    ),
                ],
                "invalid": [],
            }
        if method == "list_presets":
            return {
                "presets": [
                    {
                        "name": "balanced",
                        "description": "Even throughput/latency trade-off for chat serving.",
                        "engine": {"gpu_memory_utilization": 0.9},
                    },
                    {
                        "name": "throughput",
                        "description": "Batch-heavy serving: larger max-num-seqs, prefix cache.",
                        "engine": {"max_num_seqs": 64},
                    },
                ]
            }
        if method == "list_deployment_recipes":
            return {"recipes": []}
        if method == "list_models":
            return {
                "models": [
                    {
                        "model_id": "qwen3-bf16-pin",
                        "display_name": "qwen3-bf16-pin",
                        "repo_id": "Qwen/Qwen3-8B",
                        "revision": "main",
                        "status": "verified",
                        "source": "hf",
                    }
                ]
            }
        if method == "list_builds":
            return {
                "builds": [
                    {
                        "build_id": "01NIGHTLY",
                        "label": "nightly-cu130-sm120",
                        "status": "ready",
                        "active": True,
                        "method": "nightly",
                        "resolved": {"vllm": "0.20.2rc1", "cuda": "13.0"},
                    },
                    {
                        "build_id": "01PIPSTABLE",
                        "label": "pip-stable",
                        "status": "ready",
                        "active": False,
                        "method": "pip",
                        "resolved": {"vllm": "0.11.2", "cuda": "12.8"},
                    },
                ],
                "skipped": [],
            }
        if method == "preview":
            return {
                "preview": (
                    "docker run -d --name qwen3-8b-bf16-vela --gpus all "
                    "--network host -e CUDA_VISIBLE_DEVICES -e VLLM_API_KEY "
                    "vllm/vllm-openai@sha256:"
                    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046 "
                    "Qwen/Qwen3-8B "
                    "--served-model-name qwen3-8b-bf16 --host 0.0.0.0 --port 18002 "
                    "--gpu-memory-utilization 0.95 --max-model-len 32768 "
                    "--dtype bfloat16\nVLLM_API_KEY='••••'"
                ),
                "warnings": [
                    "Binds vLLM to 0.0.0.0, reachable beyond localhost; put it behind "
                    "a reverse proxy or firewall."
                ],
            }
        if method == "list_targets":
            return {"targets": []}
        return {}


async def _capture(out_dir: Path) -> None:
    configs_dir = Path(_tmp_state) / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "qwen3-8b-bf16.yaml").write_text(QWEN_CONFIG, encoding="utf-8")
    (configs_dir / "tiny-llama-smoke.yaml").write_text(TINY_CONFIG, encoding="utf-8")

    app = VelaApp(
        configs_dir=configs_dir,
        target_client=ScreenshotClient(configs_dir),
        target_name="gpu-node",
        target_ping_interval_seconds=None,
    )
    async with app.run_test(size=(144, 42)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await asyncio.sleep(0.2)
        await pilot.pause()
        app.save_screenshot(str(out_dir / "dashboard.svg"))

        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        app.save_screenshot(str(out_dir / "new-deployment.svg"))
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("b")
        await pilot.pause()
        await pilot.pause()
        app.save_screenshot(str(out_dir / "build-manager.svg"))
        await pilot.press("escape")
        await pilot.pause()


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/img")
    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_capture(out_dir))
    for name in ("dashboard.svg", "new-deployment.svg", "build-manager.svg"):
        path = out_dir / name
        status = "ok" if path.exists() else "MISSING"
        print(f"{status}  {path}")


if __name__ == "__main__":
    main()
