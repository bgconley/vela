# Mac to GPU Workflow

This project is expected to be authored on a Mac and exercised for real vLLM
runtime behavior on GPU boxes. Local Mac validation should stay no-GPU and
no-vLLM by default.

## 1. Publish the tree

```bash
git status --short
git add -A
git commit -m "describe the validation change"
git push origin main
```

Before any new GPU-node test, commit locally and push the commit to the remote.
The GPU host should be a normal clone of this repo; `scripts/run_remote_tests.sh`
runs `git pull --ff-only origin main` on the GPU node before it installs the
editable package or starts validation. Machine-specific secrets should stay on
the GPU host. Do not put `HF_TOKEN` or API keys in example configs.

If the GPU host needs a specific SSH key or options, set them for validation:

```bash
export VLLM_LOADER_SSH_OPTS="-i /path/to/gpu_key -o BatchMode=yes"
```

## 2. Run remote validation

No-GPU-safe validation on the GPU host:

```bash
scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
```

This installs the editable package, prints host/GPU/vLLM diagnostics, runs
Ruff/pytest, and checks the fake config preview path.

The remote script creates or reuses a persistent ZFS-backed validation
environment at `/tank/venvs/lab-tui` by default, then installs this package into
that venv. Override the venv path only when the host has a different ZFS layout:

```bash
VLLM_LOADER_REMOTE_VENV=/tank/venvs/custom-lab-tui \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
```

The venv is created with `/tank/preproc/venv/bin/python` when that seed
interpreter exists, otherwise `python3`, then `python`. Override the seed
interpreter when the GPU box needs a specific venv-capable Python:

```bash
VLLM_LOADER_REMOTE_PYTHON=/path/to/venv/bin/python \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
```

That interpreter must be able to create a pip-enabled venv; otherwise install
`python3-venv`/`ensurepip` support or point `VLLM_LOADER_REMOTE_PYTHON` at a
prepared environment.

Real vLLM validation with a named config already present in the committed
`configs/` directory or the host's configured config directory:

```bash
scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui my-real-config
```

Real managed-artifact validation is opt-in so the default remote run stays safe
and reasonably fast. To exercise the build installer, provide a pip spec; the
script runs `vllm-loader build add` followed by `build verify` on the GPU host:

```bash
VLLM_LOADER_REMOTE_BUILD_SPEC='vllm==0.11.2' \
VLLM_LOADER_REMOTE_BUILD_LABEL=real-build-smoke \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
```

To exercise the model download path, pin a small Hugging Face repo and download
it through the agent. Keep `HF_TOKEN` on the GPU host if the repo is gated:

```bash
VLLM_LOADER_REMOTE_MODEL_ID=real-model-smoke \
VLLM_LOADER_REMOTE_MODEL_REPO=hf-internal-testing/tiny-random-LlamaForCausalLM \
VLLM_LOADER_REMOTE_MODEL_REVISION=main \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
```

These knobs can be combined with the real config smoke. In that case the script
first runs no-GPU checks, then the optional build/model artifact jobs, then
`preview` + `smoke-tui` for the named config.

On a busy controller host where the full no-GPU suite can conflict with local
services, use `VLLM_LOADER_REMOTE_PYTEST_ARGS` to run a narrower proof slice
before the real target operations:

```bash
VLLM_LOADER_REMOTE_PYTEST_ARGS="-q tests/test_remote_workflow.py" \
  scripts/run_remote_tests.sh USER@CONTROLLER /home/user/repos/lab-tui
```

When the validation host is a controller that should drive a different target
agent, set `VLLM_LOADER_REMOTE_TARGET`. The script passes `--target` to the
build/model jobs and the real-config preview/smoke commands:

```bash
VLLM_LOADER_REMOTE_TARGET=blackbird \
  scripts/run_remote_tests.sh bgconley@10.25.0.50 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

To write the reviewable validation record required for pre-release signoff, use
the same command with artifact capture enabled. The script records the local
commit, target host/path, optional build/model/config knobs, full remote output,
and final exit status in a fresh Markdown file:

```bash
VLLM_LOADER_REMOTE_ARTIFACT=1 \
VLLM_LOADER_REMOTE_ARTIFACT_DIR=artifacts/remote-validation \
VLLM_LOADER_REMOTE_BUILD_SPEC='vllm==0.11.2' \
VLLM_LOADER_REMOTE_MODEL_ID=real-model-smoke \
VLLM_LOADER_REMOTE_MODEL_REPO=hf-internal-testing/tiny-random-LlamaForCausalLM \
VLLM_LOADER_REMOTE_MODEL_REVISION=main \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui my-real-config
```

The same lane is available as the GitHub Actions workflow `Remote Validation`.
It runs `scripts/run_remote_tests.sh` instead of grepping for script text and
uploads the generated Markdown under `remote-validation-artifacts`. The workflow
supports manual dispatch and a nightly schedule on a self-hosted runner. It has
a concurrency group so the validation lane does not double-book the Blackwell.
Use the `full` profile for Qwen smoke plus real resume/restart validation, and
the `fast` profile for build/model proof without the long Qwen/resume pass. If
the runner needs a private key, store it as the `VLLM_LOADER_REMOTE_SSH_KEY`
secret. Keep this workflow restricted to trusted branches/tags/manual use; do
not run untrusted fork PR code on a GPU host with lab-network access.

To cover the final real-model reconnect surface, set
`VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG` to a real, non-fake detached config. The
default workflow uses `tiny-random-llama-detached-blackbird`, which launches the
tiny HF Llama model through the just-installed build and downloaded model
registry entry. The script launches that config through the selected target,
disconnects and resumes by log cursor, restarts the target daemon while the
model is still live, rediscovers and reattaches the run, verifies health, then
stops it:

```bash
VLLM_LOADER_REMOTE_TARGET=blackbird \
VLLM_LOADER_REMOTE_BUILD_SPEC=vllm==0.11.2 \
VLLM_LOADER_REMOTE_BUILD_LABEL=p620-target-vllm-0112 \
VLLM_LOADER_REMOTE_MODEL_ID=p620-target-tiny-llama \
VLLM_LOADER_REMOTE_MODEL_REPO=hf-internal-testing/tiny-random-LlamaForCausalLM \
VLLM_LOADER_REMOTE_MODEL_REVISION=main \
VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG=tiny-random-llama-detached-blackbird \
  scripts/run_remote_tests.sh bgconley@10.25.0.50 /home/bgconley/repos/lab-tui
```

Preferred real architecture smoke: P620-01 controller to Blackbird agent. The
controller host is `620-01` (`10.25.0.50`), and the GPU/agent target is
`blackbird` (`10.25.0.51`) with the `RTX PRO 6000 Blackwell Max-Q` GPU and
Qwen3.6 27B FP8. Use SSH agent forwarding from the Mac unless the shared key is
installed directly on P620:

```bash
VLLM_LOADER_REMOTE_VENV=/home/bgconley/venvs/lab-tui \
VLLM_LOADER_REMOTE_TIMEOUT=2700 \
VLLM_LOADER_REMOTE_TARGET=blackbird \
  scripts/run_remote_tests.sh bgconley@10.25.0.50 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'cd /home/bgconley/repos/lab-tui &&
   /home/bgconley/venvs/lab-tui/bin/vllm-loader targets test blackbird'
```

Then run the real TUI smoke from P620 through the `blackbird` target:

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'cd /home/bgconley/repos/lab-tui &&
   timeout 2700 /home/bgconley/venvs/lab-tui/bin/vllm-loader smoke-tui \
     qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird'
```

The remote command invokes
`vllm-loader smoke-tui qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird`
from P620.

The latest P620-to-Blackbird validation records are:

- `artifacts/remote-validation/2026-06-04-p620-blackbird-b085610-build-model-resume.md`
  for the managed vLLM build install, tiny HF model pin/download, and real
  model resume/daemon restart pass.
- `artifacts/remote-validation/2026-06-04-p620-blackbird-b085610-qwen-smoke.md`
  for the Qwen3.6 27B FP8 `smoke-tui` pass through the Blackbird target.

Direct Mac to Blackbird validation is still useful for host-local checks:

```bash
git push origin main
VLLM_LOADER_REMOTE_VENV=/home/bgconley/venvs/lab-tui \
  scripts/run_remote_tests.sh bgconley@10.25.0.51 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

That config uses a repo-local foreground Docker wrapper,
`./scripts/blackbird_qwen36_vllm_foreground.sh`, to launch the pinned
`vllm/vllm-openai` image for `Qwen/Qwen3.6-27B-FP8`, stream container logs into
the TUI, and stop the container when the TUI Stop flow runs. The config serves
`qwen36-27b-fp8-kvfp8-rp6000` on host port `18003` and probes localhost. It may
stop conflicting Blackbird Qwen containers while active, because the RP6000 GPU
cannot host the full test lane alongside another large model.

Historical/fallback real smoke target: `620-01` (`10.25.0.50`) with Qwen3-32B
FP8:

```bash
scripts/run_remote_tests.sh bgconley@10.25.0.50 /tank/repos/lab-tui qwen3-32b-fp8-62001
```

That config uses `/tank/triton/venv-vllm/bin/vllm` directly and serves
`/tank/trt/models/Qwen3-32B-FP8` on `127.0.0.1:8017` with two visible GPUs.
The remote validation venv does not install vLLM, so the diagnostic line may
say `vllm not found on PATH`; the real config preview/smoke still validates the
absolute lab vLLM executable path.

The real run uses `vllm-loader smoke-tui`: it mounts the Textual app headlessly,
selects the config, follows the normal Load workflow, waits for READY via the
app's health/model state, prints the READY URL/model names, then follows the
normal Stop workflow. It is still wrapped in `timeout` as a hard guard. Override
the limit with:

```bash
VLLM_LOADER_REMOTE_TIMEOUT=2400 scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui my-real-config
```

## 3. Tested vLLM surface

The preferred Blackbird lane is based on the validated Qwen3.6 27B FP8 Docker
stack from `10.25.0.51`: vLLM `0.20.2rc1.dev9+g01d4d1ad3`, Transformers `5.7.0`,
Torch `2.11.0+cu130`, FP8 KV, FlashInfer attention, and Cutlass FP8 GEMM.

The fallback 620-01 lane covers the tested vLLM 0.19 lab surface and was
verified with vLLM
`v0.19.1rc1.dev119+gba4a78eb5` from `/tank/triton/venv-vllm/bin/vllm`. Treat
these as the tested lab surfaces, not a promise that older or newer vLLM builds
emit the same flags and log strings. When bumping a lab vLLM build, rerun the
real smoke and add or adjust recorded log fixtures and `VllmProfile` rules for
any changed startup, download, readiness, or error text.

## 4. Browser access through Textual

For browser access on a GPU host, use Textual's own `textual serve` entrypoint
around `vllm-loader` only on a trusted network/auth boundary. The served TUI
controls model launches, stops, kills, and log access; do not expose it as an
unauthenticated public service.

## 5. Where results land

By default, `vllm-loader run` writes scrubbed run artifacts on the GPU host:

```text
~/.local/state/vllm-loader/runs/
```

The durable log contains scrubbed committed lines only. Transient carriage-return
progress frames are shown in the UI/process stream but are not persisted.
