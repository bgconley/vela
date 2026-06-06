# P620 -> Blackbird Native Docker FP8 Validation

- Date: 2026-06-06
- Commit: `d67b3a6`
- Controller: P620-01 (`10.25.0.50`)
- Target agent: Blackbird (`10.25.0.51`)
- Config: `qwen36-27b-fp8-kvfp8-rp6000-blackbird`
- Runtime: native `command.runtime: docker`
- Image: `vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046`

## Preconditions

- P620 target registry included `blackbird` via SSH:
  - `host: bgconley@10.25.0.51`
  - `workdir: /home/bgconley/repos/lab-tui`
  - `venv: /home/bgconley/venvs/lab-tui`
- P620 and Blackbird repos were pulled to `d67b3a6`.
- Blackbird venv exposed the `vela` entrypoint.
- Blackbird pinned image was already present.
- Blackbird GPU/port precheck:
  - GPU: `2 MiB / 97887 MiB`, `0 %`
  - Port `18003`: no listener

## Command

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'cd /home/bgconley/repos/lab-tui && timeout 2700 \
   /home/bgconley/venvs/lab-tui/bin/python -m vela.cli smoke-tui \
   qwen36-27b-fp8-kvfp8-rp6000-blackbird \
   --configs-dir configs --target blackbird'
```

## Result

The TUI smoke reached READY and exited successfully:

```text
READY http://10.25.0.51:18003 models=qwen36-27b-fp8-kvfp8-rp6000
```

Remote run artifact:

```text
/home/bgconley/models/qwen36-27b-fp8-rp6000/vela-runs/26d7d48629724d39a1f6a15383a7c35e.exit-status
{
  "run_id": "26d7d48629724d39a1f6a15383a7c35e",
  "returncode": 0,
  "exited_at": "2026-06-06T10:35:09Z"
}
```

Post-run Blackbird state:

```text
qwen36-27b-fp8-kvfp8-rp6000-vela    Exited (0)
port 18003                          no listener
GPU                                 2 MiB / 97887 MiB, 0 %
```

## Local Verification

```text
ruff check .                         All checks passed
pytest -q                            799 passed in 101.91s
```

