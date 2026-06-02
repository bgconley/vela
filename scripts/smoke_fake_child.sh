#!/usr/bin/env bash
set -euo pipefail

python -m pip install -e ".[dev]"
vllm-loader list
vllm-loader preview fake-child
pytest -q tests/test_process_manager.py tests/test_tui_smoke.py
