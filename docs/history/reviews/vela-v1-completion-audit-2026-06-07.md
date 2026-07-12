# Vela v1 Completion Audit - 2026-06-07

Audit point: `1937896 Add new deployment acceptance flow test`

This audit records the completion state before the doc-only audit commit that
adds this file. It is intentionally scoped: Vela v1 is complete for the
registered/proven lab recipes and the TUI/onboarding workflows in
`vela-v1-completion-punchlist.md`. It is not a promise that Vela can derive any
Blackwell deployment from a Hugging Face model card.

## Runtime Authority Boundary

For Blackwell targets, local deployment scripts and real run records are the
runtime authority. Hugging Face metadata is useful for model identity,
revision/cache state, gated-token warnings, and broad defaults. It must not
choose the vLLM image, `sm_120`/FlashInfer arch, CUTLASS/FlashInfer/
FlashAttention backend shape, cache layout, or FP8/BF16 KV memory layout.

The current registered Qwen3.6 Blackbird recipes are anchored to:

- `scripts/blackbird_qwen36_vllm_foreground.sh`
- `scripts/blackbird_qwen36_bf16_vllm_foreground.sh`
- `configs/qwen36-27b-fp8-kvfp8-rp6000-blackbird.yaml`
- `configs/qwen36-27b-bf16-rp6000-blackbird.yaml`
- `/Users/brennanconley/vibecode/infx/qwen36-27b-test/start-qwen36-27b-fp8-rp6000-blackbird.sh`
- `/Users/brennanconley/vibecode/infx/qwen36-27b-test/start-qwen36-bf16-rp6000-blackbird.sh`

Key preserved runtime facts:

- Docker image digest:
  `vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046`
- Proven stack metadata recorded in configs:
  vLLM `0.20.2rc1.dev9+g01d4d1ad3`, Transformers `5.7.0`,
  Torch `2.11.0+cu130`, CUDA `13.0`
- FP8 recipe: `FLASHINFER_CUDA_ARCH_LIST=12.0f`,
  `--attention-backend FLASHINFER`, `kv_cache_dtype: fp8`,
  `--kv-cache-memory-bytes 64424509440`, CUDA graph config, FlashInfer cache
  mount, and Cutlass/FlashInfer backend evidence requirement
- BF16 recipe: same pinned image, `kv_cache_dtype: bfloat16`, no FP8 KV-byte
  cap, and no FP8-only FlashInfer arch pin
- P620 Qwen3-32B FP8 fallback: host-local executable
  `/tank/triton/venv-vllm/bin/vllm`, not a synthesized Docker recipe

Guardrails verified in code/tests:

- Registered lab recipes ignore conflicting requested Docker images and emit
  `recipe-image-override-ignored`.
- Recipe-less Blackbird/P620 Docker FP8 composition fail-closes with
  `blackwell-fp8-runtime-recipe-required`.
- Suggestion surfaces may warn, but final compose refuses unsafe FP8 Docker
  shape without a matched lab recipe.
- Backend evidence for the FP8 recipe requires Cutlass FP8 and FlashInfer
  attention and rejects MARLIN fallback.
- Backend evidence for the BF16 recipe verifies the pinned image and forbids the
  FP8-only KV cap/FlashInfer arch pin.
- Docs explicitly state that the composer must not infer Blackwell runtime
  shape from Hugging Face metadata.

## Track A - Core Engine

Status: complete for v1, with one intentional process deviation.

Closed:

- A1 clone secret-at-rest bypass
- A2/A3 backend evidence shape coverage and unregistered-config fail-close
- A4 edit-config literal secret coverage
- A5/A6 offline remote-only pin behavior and repo-not-found taxonomy
- A7 Docker discover/reattach across fresh agent and stale identity refusal
- A8/A9 Docker error kinds and TUI remediation guidance
- A10 explicit overwrite semantics for `deploy create`
- A11 generic `--ipc=host`/computed `--shm-size` fix while preserving explicit
  Blackbird `shm_size: 32g`
- A12 exposure mismatch remains warn-not-block
- A13 dedup-aware model removal display
- A15 structured smoke run-id marker and restart-lane backend evidence gate

A14 note:

The required FP8/BF16 P620-to-Blackbird hardware proofs exist with READY and
`BACKEND_EVIDENCE_OK`. The foreground wrappers are intentionally retained as
provenance/manual comparison artifacts because the local scripts remain the
authority for the Blackwell stack shape.

Latest hardware artifacts used:

- `artifacts/remote-validation/2026-06-07T07-49-52Z-bgconley-10.25.0.50-qwen36-27b-fp8-kvfp8-rp6000-blackbird-remote-validation.md`
- `artifacts/remote-validation/2026-06-07T07-54-32Z-bgconley-10.25.0.50-qwen36-27b-bf16-rp6000-blackbird-remote-validation.md`
- `artifacts/remote-validation/2026-06-07T07-38-08Z-bgconley-10.25.0.50-remote-validation.md`

The later commits after those hardware runs are low-tail docs/tests/TUI target
picker/acceptance-test changes. They do not alter the pinned Blackwell launch
shape, Docker runtime, recipe constants, or backend evidence contract.

## Track B - Onboarding

Status: complete for v1 except the P4 target-manager affordance, deferred with
rationale.

Closed:

- B0 fake-SSH/fake-remote harness
- B1 SSH agent discovery probe
- B2 named remediations with exact target-specific commands
- B3 real `targets bootstrap --install --build` flow
- B4 real `doctor --target --json` two-host diagnosis with conditional next
  steps
- B5 resolved path visibility in doctor/status/targets-test
- B6 guided `targets setup-ssh`
- B7 `agent gen-token --install --target`
- B8 `build doctor --target`
- B9 target `config edit`
- B10 five-state auth reporting coverage
- B12 canonical install path/remote install job

Deferred:

- B11 TUI "Bootstrap target..." / "Push config..." affordance remains a P4
  target-manager improvement. It needs explicit overwrite/confirmation semantics
  to avoid clobbering target configs and is not required for the v1 acceptance
  commands.

Acceptance evidence:

- Fake-SSH bootstrap with `--install --build` reached handshake and build-ready
  state.
- `doctor --target` fake acceptance returned `ok: true` with target connection,
  paths, auth, and active build/model state.

## Track C - TUI Primary Surface

Status: complete for v1.

Closed:

- C1 New Deployment create-build and adopt-venv handoffs
- C2 model step with existing pin, pin HF repo + revision, adopt local path,
  bare repo, download-now, and gated/cached state display
- C3 target registry picker with live/non-active connection dots
- C4 FlagManager presets, reset-to-preset, and changed-only filter
- C5 live deployment-default suggestions and warnings
- C6 review/customize/save/smoke bounded Docker walk
- C7 named smoke failures preserve ErrorKind remediation

Whole-handoff TUI acceptance:

- `test_new_deployment_build_pin_and_smoke_acceptance_flow` drives a single
  wizard flow through create build, pin model, download-now, compose, save,
  launch bounded smoke, READY, stop, and wait.

## Verification Snapshot

Commands completed at audit point:

- `PYTHONPATH=src python -m pytest -q -p no:randomly`
  - `954 passed, 5 warnings in 157.35s`
- `ruff check .`
  - clean
- `git diff --check`
  - clean
- Crown-jewel grep:
  - `rg -n "Popen|os\\.kill|killpg|pynvml|snapshot_download|docker\\.from_env|import subprocess" src/vela/tui/app.py src/vela/cli.py`
  - no matches
- Fake-SSH bootstrap acceptance:
  - `OK ssh reachable`
  - `OK agent installed`
  - `OK target wrote .../targets.yaml`
  - `OK handshake agent=0.1.0`
  - `DONE ... build ready`
- Fake doctor acceptance:
  - `DOCTOR_ACCEPTANCE_OK True`
  - target connection, paths, auth, and active build/model all green
- New Deployment subset:
  - `24 passed`
- Combined New Deployment acceptance:
  - `1 passed`

## Final Read

Vela v1 is ready as a TUI-first controller/agent application for the proven lab
deployment surfaces:

- P620-01 can run the TUI/controller.
- P620-01 can target its own local agent.
- P620-01 can target Blackbird over SSH through the same TargetClient/RPC
  boundary.
- The TUI remains transport-agnostic and does not hold process, Docker, sidecar,
  or target-local path handles.
- Blackwell Docker deployments are recipe-led. New model families should be
  added by importing/registering reviewed local deployment scripts and then
  proving them on hardware, not by deriving runtime shape from Hugging Face
  metadata.
