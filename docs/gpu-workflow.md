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
export VELA_SSH_OPTS="-i /path/to/gpu_key"
```

When the same environment variable is referenced by a target registry
`ssh_opts_env`, Vela manages `BatchMode` and keepalive options itself.

## 2. Run remote validation

No-GPU-safe validation on the GPU host:

```bash
scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela
```

This installs the editable package, prints host/GPU/vLLM diagnostics, runs
Ruff/pytest, and checks the fake config preview path.

The remote script creates or reuses a persistent ZFS-backed validation
environment at `/tank/venvs/vela` by default, then installs this package into
that venv. Override the venv path only when the host has a different ZFS layout:

```bash
VELA_REMOTE_VENV=/tank/venvs/custom-vela \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela
```

The venv is created with `/tank/preproc/venv/bin/python` when that seed
interpreter exists, otherwise `python3`, then `python`. Override the seed
interpreter when the GPU box needs a specific venv-capable Python:

```bash
VELA_REMOTE_PYTHON=/path/to/venv/bin/python \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela
```

That interpreter must be able to create a pip-enabled venv; otherwise install
`python3-venv`/`ensurepip` support or point `VELA_REMOTE_PYTHON` at a
prepared environment.

Real vLLM validation with a named config already present in the committed
`configs/` directory or the host's configured config directory:

```bash
scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela my-real-config
```

Real managed-artifact validation is opt-in so the default remote run stays safe
and reasonably fast. To exercise the build installer, provide a pip spec; the
script runs `vela build add` followed by `build verify` on the GPU host:

```bash
VELA_REMOTE_BUILD_SPEC='vllm==0.11.2' \
VELA_REMOTE_BUILD_LABEL=real-build-smoke \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela
```

To exercise the model download path, pin a small Hugging Face repo and download
it through the agent. Keep `HF_TOKEN` on the GPU host if the repo is gated:

```bash
VELA_REMOTE_MODEL_ID=real-model-smoke \
VELA_REMOTE_MODEL_REPO=hf-internal-testing/tiny-random-LlamaForCausalLM \
VELA_REMOTE_MODEL_REVISION=main \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela
```

To exercise the real gated/auth path without downloading a gated model, set a
known gated repo as a negative probe. The script launches an isolated local
agent on the validation host with `HF_HUB_DISABLE_IMPLICIT_TOKEN=1`, an empty
`HF_TOKEN`, and a temporary state/cache root, then expects the normal
`download_model` job to end with `gated-auth` and print `GATED_MODEL_AUTH_OK`:

```bash
VELA_REMOTE_GATED_MODEL_REPO=meta-llama/Llama-2-7b-hf \
VELA_REMOTE_GATED_MODEL_ID=gated-llama-auth \
VELA_REMOTE_GATED_MODEL_REVISION=main \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela
```

These knobs can be combined with the real config smoke. In that case the script
first runs no-GPU checks, then the optional build/model/gated-auth artifact
jobs, then `preview` + `smoke-tui` for the named config.

On a busy controller host where the full no-GPU suite can conflict with local
services, use `VELA_REMOTE_PYTEST_ARGS` to run a narrower proof slice
before the real target operations:

```bash
VELA_REMOTE_PYTEST_ARGS="-q tests/test_remote_workflow.py" \
  scripts/run_remote_tests.sh USER@CONTROLLER /home/user/repos/vela
```

When the validation host is a controller that should drive a different target
agent, set `VELA_REMOTE_TARGET`. The script passes `--target` to the
build/model jobs and the real-config preview/smoke commands:

```bash
VELA_REMOTE_TARGET=blackbird \
  scripts/run_remote_tests.sh bgconley@10.25.0.50 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

To write the reviewable validation record required for pre-release signoff, use
the same command with artifact capture enabled. The script records the local
commit, target host/path, optional build/model/config knobs, full remote output,
and final exit status in a fresh Markdown file:

```bash
VELA_REMOTE_ARTIFACT=1 \
VELA_REMOTE_ARTIFACT_DIR=artifacts/remote-validation \
VELA_REMOTE_BUILD_SPEC='vllm==0.11.2' \
VELA_REMOTE_MODEL_ID=real-model-smoke \
VELA_REMOTE_MODEL_REPO=hf-internal-testing/tiny-random-LlamaForCausalLM \
VELA_REMOTE_MODEL_REVISION=main \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela my-real-config
```

The same lane is available as the GitHub Actions workflow `Remote Validation`.
It runs `scripts/run_remote_tests.sh` instead of grepping for script text and
uploads the generated Markdown under `remote-validation-artifacts`. The workflow
supports manual dispatch and a nightly schedule on a self-hosted runner. It has
a concurrency group so the validation lane does not double-book the Blackwell.
Use the `full` profile for Qwen smoke plus real resume/restart validation, and
the `fast` profile for build/model proof without the long Qwen/resume pass. If
the runner needs a private key, store it as the `VELA_REMOTE_SSH_KEY`
secret. Keep this workflow restricted to trusted branches/tags/manual use; do
not run untrusted fork PR code on a GPU host with lab-network access. The
workflow also accepts `gated_model_repo`, `gated_model_id`, and
`gated_model_revision` inputs, or the matching
`VELA_REMOTE_GATED_MODEL_REPO` repo variable family, for the no-token
gated auth probe.

To cover the final real-model reconnect surface, set
`VELA_REMOTE_REAL_RESUME_CONFIG` to a real, non-fake detached config. The
default workflow uses `tiny-random-llama-detached-blackbird`, which launches the
tiny HF Llama model through the just-installed build and downloaded model
registry entry. The script launches that config through the selected target,
disconnects and resumes by log cursor, restarts the target daemon while the
model is still live, rediscovers and reattaches the run, verifies health, then
stops it:

```bash
VELA_REMOTE_TARGET=blackbird \
VELA_REMOTE_BUILD_SPEC=vllm==0.11.2 \
VELA_REMOTE_BUILD_LABEL=p620-target-vllm-0112 \
VELA_REMOTE_MODEL_ID=p620-target-tiny-llama \
VELA_REMOTE_MODEL_REPO=hf-internal-testing/tiny-random-LlamaForCausalLM \
VELA_REMOTE_MODEL_REVISION=main \
VELA_REMOTE_REAL_RESUME_CONFIG=tiny-random-llama-detached-blackbird \
  scripts/run_remote_tests.sh bgconley@10.25.0.50 /home/bgconley/repos/lab-tui
```

Preferred real architecture smoke: P620-01 controller to Blackbird agent. The
controller host is `620-01` (`10.25.0.50`), and the GPU/agent target is
`blackbird` (`10.25.0.51`) with the `RTX PRO 6000 Blackwell Max-Q` GPU and
Qwen3.6 27B FP8. If running the helper from a Mac that does not have the shared
lab key installed on P620, SSH agent forwarding may be used only for the outer
Mac-to-P620 shell session. Do not put `-A` or `ForwardAgent=yes` in a target's
`ssh_opts_env`; Vela disables agent forwarding for the controller-to-agent
NDJSON transport.

```bash
VELA_REMOTE_VENV=/tank/venvs/lab-tui \
VELA_REMOTE_TIMEOUT=2700 \
VELA_REMOTE_TARGET=blackbird \
  scripts/run_remote_tests.sh bgconley@10.25.0.50 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'cd /home/bgconley/repos/lab-tui &&
   /tank/venvs/lab-tui/bin/vela targets test blackbird'
```

Then run the real TUI smoke from P620 through the `blackbird` target:

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'cd /home/bgconley/repos/lab-tui &&
   timeout 2700 /tank/venvs/lab-tui/bin/vela smoke-tui \
     qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird'
```

The remote command invokes
`vela smoke-tui qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird`
from P620.

The latest P620-to-Blackbird validation records are:

- `artifacts/remote-validation/2026-06-06-p620-blackbird-native-docker-fp8-d67b3a6.md`
  for the native `command.runtime: docker` Qwen3.6 27B FP8 smoke on Blackbird.
- `artifacts/remote-validation/2026-06-06-p620-blackbird-native-docker-bf16-9b107b4.md`
  for the native `command.runtime: docker` Qwen3.6 27B BF16 smoke on Blackbird.
- `artifacts/remote-validation/2026-06-04T20-04-41Z-bgconley-10.25.0.50-qwen36-27b-fp8-kvfp8-rp6000-blackbird-remote-validation.md`
  from GitHub Actions run `26976430928`, produced by the P620 self-hosted
  runner. It covers the managed vLLM build install, tiny HF model pin/download,
  Qwen3.6 27B FP8 `smoke-tui`, and real model resume/daemon restart pass
  through the Blackbird target.
- `artifacts/remote-validation/2026-06-04T20-34-19Z-bgconley-10.25.0.50-remote-validation.md`
  from the P620 validation host at commit `d90ec83`. It covers the opt-in
  no-token gated Hugging Face auth probe against `meta-llama/Llama-2-7b-hf`,
  with the normal agent `download_model` job ending in `GATED_MODEL_AUTH_OK`.

Physical controller sleep is intentionally not triggered by automation. In the
lab topology, P620-01 is the controller host and Blackbird/P620-01 are target
agents. Sleeping the Mac while it is only an SSH terminal into P620 tests the
outer operator session, not the TUI controller-to-agent boundary. Use `tmux`,
`screen`, `systemd-run`, or another session manager for that operator-shell
case.

To run the stricter controller sleep drill, start the controller on the machine
that should sleep, launch a detached real config, and let the script hold the
original connection open while you sleep and wake that controller. After wake,
press Enter; the script disconnects, reconnects, rediscovers the live sidecar,
reattaches, resumes by log cursor, and writes `LAPTOP_SLEEP_RECONNECT_OK` to an
optional artifact:

```bash
scripts/laptop_sleep_reconnect_check.py tiny-random-llama-detached-blackbird \
  --target blackbird \
  --build gha-26976430928-1-build \
  --model-ref gha-26976430928-1-model \
  --timeout 900 \
  --artifact-dir artifacts/remote-validation
```

Do not run this from an unsupervised CI job; run the script on the actual
controller host for the stricter drill.
If the selected target venv already has a compatible `vllm` on `PATH`, the
`--build`/`--model-ref` overrides can be omitted; the Blackbird validation lane
uses the managed build/model labels shown above.

Direct Mac to Blackbird validation is still useful for host-local checks:

```bash
git push origin main
VELA_REMOTE_VENV=/home/bgconley/venvs/lab-tui \
  scripts/run_remote_tests.sh bgconley@10.25.0.51 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

That config now uses native `command.runtime: docker`: the Blackbird agent
generates the pinned `vllm/vllm-openai` `docker run`, streams `docker logs -f`
into the TUI, waits on `docker wait`, and stops with `docker stop -t`. The
config serves `qwen36-27b-fp8-kvfp8-rp6000` on host port `18003` and probes
localhost. It may stop conflicting Blackbird Qwen containers while active,
because the RP6000 GPU cannot host the full test lane alongside another large
model.

Historical/fallback real smoke target: `620-01` (`10.25.0.50`) with Qwen3-32B
FP8:

```bash
scripts/run_remote_tests.sh bgconley@10.25.0.50 /tank/repos/vela qwen3-32b-fp8-62001
```

That config uses `/tank/triton/venv-vllm/bin/vllm` directly and serves
`/tank/trt/models/Qwen3-32B-FP8` on `127.0.0.1:8017` with two visible GPUs.
The remote validation venv does not install vLLM, so the diagnostic line may
say `vllm not found on PATH`; the real config preview/smoke still validates the
absolute lab vLLM executable path.

The real run uses `vela smoke-tui`: it mounts the Textual app headlessly,
selects the config, follows the normal Load workflow, waits for READY via the
app's health/model state, prints the READY URL/model names, then follows the
normal Stop workflow. It is still wrapped in `timeout` as a hard guard. Override
the limit with:

```bash
VELA_REMOTE_TIMEOUT=2400 scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/vela my-real-config
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
around `vela` only on a trusted network/auth boundary. The served TUI
controls model launches, stops, kills, and log access; do not expose it as an
unauthenticated public service.

## 5. Where results land

By default, `vela run` writes scrubbed run artifacts on the GPU host:

```text
~/.local/state/vela/runs/
```

The durable log contains scrubbed committed lines only. Transient carriage-return
progress frames are shown in the UI/process stream but are not persisted.
