# P620 -> Blackbird Native Docker BF16 Validation

- Date: 2026-06-06
- Commit: `9b107b4`
- Controller: P620-01 (`10.25.0.50`)
- Target agent: Blackbird (`10.25.0.51`)
- Config: `qwen36-27b-bf16-rp6000-blackbird`
- Runtime: native `command.runtime: docker`
- Image: `vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046`

## Preconditions

- P620 and Blackbird repos were pulled to `9b107b4`.
- P620 target registry included `blackbird` via SSH.
- Blackbird pinned image was already present.
- Blackbird GPU/port precheck:
  - GPU: `2 MiB / 97887 MiB`, `0 %`
  - Port `18002`: no listener
- BF16 preview preserved the wrapper-derived BF16 shape:
  - root mount: `/home/bgconley/models/qwen36-27b-bf16:/home/bgconley/models/qwen36-27b-bf16`
  - no FP8 `--kv-cache-memory-bytes`
  - no FP8 `FLASHINFER_CUDA_ARCH_LIST`

## Command

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'cd /home/bgconley/repos/lab-tui && timeout 2700 \
   /home/bgconley/venvs/lab-tui/bin/python -m vela.cli smoke-tui \
   qwen36-27b-bf16-rp6000-blackbird \
   --configs-dir configs --target blackbird'
```

## Result

The TUI smoke reached READY and exited successfully:

```text
READY http://10.25.0.51:18002 models=qwen36-27b-bf16-rp6000
```

Remote run artifact:

```text
/home/bgconley/models/qwen36-27b-bf16/vela-runs/2fc7f08b2ba6400b842b598eeb027b90.exit-status
{
  "run_id": "2fc7f08b2ba6400b842b598eeb027b90",
  "returncode": 0,
  "exited_at": "2026-06-06T10:39:25Z"
}
```

Post-run Blackbird state:

```text
qwen36-27b-bf16-rp6000-vela    Exited (0)
port 18002                     no listener
GPU                            2 MiB / 97887 MiB, 0 %
```

