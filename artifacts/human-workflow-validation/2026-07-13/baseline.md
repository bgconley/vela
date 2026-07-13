# Human Workflow Validation Baseline

Captured 2026-07-13 before follow-on profile-fidelity changes.

## Code and gates

- Branch: `remediate/2026-07-09-review`
- Baseline HEAD: `df860c370ec49f9c916af46f130fba91f86cc21b`
- Required interpreter: `/opt/homebrew/opt/python@3.11/libexec/bin/python3` (`Python 3.11.14`)
- Full baseline: `1437 passed in 269.74s`
- A scratch visual-QA venv produced two Typer compatibility failures because its Typer lacks `typer.edit`; the same tests passed under the required Homebrew environment. This was not classified as a product regression.

## Visible saved-profile pilot

The real Textual application was served in the visible in-app browser. A human-style wizard journey:

1. Created deployment `human-profile-reload`.
2. Chose Pin HF repo and pinned `hf-internal-testing/tiny-random-LlamaForCausalLM` from `main`.
3. The agent resolved immutable commit `9fb191250dd56d0ba7ec9785a025ed29c03d5998`.
4. Review showed the full resolved command with that commit.
5. Saved and selected the deployment.
6. Reloaded the browser, which created a fresh served TUI process.
7. Re-selected the saved profile from disk and reproduced the same model/commit/endpoint.

Scratch YAML SHA-256:

```text
2566a5ef09b54730123109e3f7e912ba5f83e4537211c879b474352823280206
```

Cold-process preview:

```text
cwd=/Users/brennanconley/vibecode/lab-tui
PYTHONUNBUFFERED=1
vllm serve hf-internal-testing/tiny-random-LlamaForCausalLM --revision 9fb191250dd56d0ba7ec9785a025ed29c03d5998 --served-model-name tiny-human-profile --host 127.0.0.1 --port 18000 --tensor-parallel-size 1 --gpu-memory-utilization 0.9 --max-model-len 2048 --dtype auto --enable-prefix-caching --enable-chunked-prefill
```

The pilot also reproduced gaps now covered by the completion plan: Process saved no immutable build; Config Picker initially selected the first config rather than the active config; non-current previews were usually absent; Pin Model had no Ctrl+S submit; and its lower preview/footer was awkward to reach at the tested viewport height.

## Oxcart before-state

- Hostname: `oxcart`
- GPU: `NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition`
- GPU memory: `2 / 97887 MiB`
- GPU utilization: `0%`
- Port `18004`: free
- Running containers (must remain unchanged outside the owned validation container):
  - `litellm-proxy`
  - `postgres-prod`
  - `postgres-dev`
  - `technitium-oxcart`
  - `qwen36-open-webui`

The live validation container will be uniquely named `vela-oxcart-qwen36-27b-fp8-mtp-vl`, use port `18004`, and may be removed only after both Vela ownership labels match.
