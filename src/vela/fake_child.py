from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STOP = threading.Event()
# Test hook: /admin/health-off makes /health return 503 while the process stays
# alive — the only way to exercise READY -> DEGRADED -> recovery end-to-end.
HEALTHY = threading.Event()
HEALTHY.set()
SERVED_MODEL_ID = "fake-model"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200 if HEALTHY.is_set() else 503)
            self.end_headers()
            self.wfile.write(b"OK" if HEALTHY.is_set() else b"unhealthy")
            return
        if self.path == "/admin/health-off":
            HEALTHY.clear()
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/admin/health-on":
            HEALTHY.set()
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {"data": [{"id": SERVED_MODEL_ID}]}
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    global SERVED_MODEL_ID

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"]:
        print("vllm 0.11.2")
        return 0
    if argv == ["serve", "--help"]:
        print_help()
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument("serve", nargs="?")
    parser.add_argument("model", nargs="?")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--served-model-name")
    parser.add_argument("--sleep", type=float, default=0.05)
    args, _unknown = parser.parse_known_args(argv)
    SERVED_MODEL_ID = args.served_model_name or args.model or "fake-model"

    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    emit_fixture(args.sleep)
    while not STOP.is_set():
        time.sleep(0.1)
    server.shutdown()
    return 0


def print_help() -> None:
    for line in [
        "usage: vllm serve [OPTIONS] MODEL",
        "  --served-model-name TEXT",
        "  --host TEXT",
        "  --port INTEGER",
        "  --tensor-parallel-size INTEGER",
        "  --pipeline-parallel-size INTEGER",
        "  --gpu-memory-utilization FLOAT",
        "  --max-model-len INTEGER",
        "  --dtype TEXT",
        "  --kv-cache-dtype TEXT",
        "  --quantization TEXT",
        "  --load-format TEXT",
        "  --swap-space INTEGER",
        "  --block-size INTEGER",
        "  --seed INTEGER",
        "  --max-num-seqs INTEGER",
        "  --enforce-eager",
        "  --enable-log-requests",
        "  --disable-log-requests",
        "  --disable-access-log-for-endpoints TEXT",
        "  --max-log-len INTEGER",
    ]:
        print(line)


def emit_fixture(delay: float) -> None:
    lines = [
        "INFO Initializing a V1 LLM engine",
        "INFO Fetching 2 files",
        "INFO Downloading model file",
        "INFO Starting to load model",
    ]
    for line in lines:
        print(line, flush=True)
        time.sleep(delay)
    for pct, done in [(25, 1), (50, 2), (75, 3), (100, 4)]:
        print(f"Loading safetensors checkpoint shards: {pct}% {done}/4", end="\r", flush=True)
        time.sleep(delay)
    print("INFO GPU KV cache size: 123 tokens", flush=True)
    print("INFO Capturing CUDA graph shapes", flush=True)
    print("INFO Uvicorn running on http://127.0.0.1:8000", flush=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
