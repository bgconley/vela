# Vela Human Workflow Completion Plan

**Status:** In execution

**Owner amendment:** For the live release proof, `oxcart` is both controller and target. The controller talks to the target through Vela's `local` transport on oxcart; no shared remote daemon may be restarted.

**Baseline:** `remediate/2026-07-09-review` at `df860c370ec49f9c916af46f130fba91f86cc21b`

## Outcome

Close the remaining gaps between the 2026-07-09 remediation plan's claimed completion and a workflow a human can trust. A saved deployment must be understandable before launch, persist every execution-affecting choice, reproduce the same resolved command after a cold controller/agent restart, launch the same immutable model/runtime identity, and clean up its owned resources.

The existing remediation definition of done remains in force. This plan adds the profile-fidelity and oxcart-local acceptance proof that the original walkthrough did not cover.

## Non-negotiable execution rules

1. Use strict red-green TDD for every code change: capture the strategic failing test, make the smallest implementation, then run the focused green test before broader gates.
2. Never infer live identity from a label. Process builds use `build_id`, HF models use the resolved full commit SHA, and Docker images use a digest.
3. Never stop, replace, or restart unrelated containers, workloads, or daemons. The live lane may evict only a validation container explicitly owned by this run.
4. Keep secrets out of YAML, logs, screenshots, and evidence. Review and preview output remain redacted.
5. A visible UI walkthrough is evidence only when it is paired with durable command/config hashes and live endpoint checks.
6. A green test suite is necessary but not sufficient. Each human acceptance row below needs observed UI evidence and a machine-verifiable result.

## Acceptance matrix

| Journey | Human-visible contract | Machine-verifiable contract |
|---|---|---|
| First run | Honest empty/offline states and guided next action | No crash; no stale target/config state |
| Create deployment | Each field explains what it controls and where its default came from | Review payload contains every launch-affecting value |
| Recipe then Custom | Custom visibly means no lab recipe and no hidden destructive behavior | No recipe-derived image, flags, mounts, or eviction list survives unless explicitly entered |
| Pin model | Repo/revision/commit relationship and cache behavior are explicit | Saved `model_ref` and full resolved SHA agree with registry/cache identity |
| Runtime selection | Process identifies an immutable build; Docker identifies an immutable image | Saved Process uses `build_id`; saved Docker uses `@sha256:` |
| Review and save | Save is distinct from launch; provenance and destructive effects are visible | Atomic YAML reloads through a new agent and validates unchanged |
| Config picker | Current profile is marked and preselected; selecting another profile is unambiguous | Temporary overrides cannot cross the config boundary |
| Use model once | UI says the override is one-shot and identifies the affected profile | Same override drives preview/preflight/prepare/launch once, then clears |
| Clone | UI states which identity/collision/ownership fields will be regenerated | All other schema fields and resolved command semantics are preserved; source-container eviction cannot carry into the clone |
| Cold restart | Saved profile can be found and selected without session memory | YAML hash, immutable identities, and normalized preview are identical |
| Launch | Verb is `Launch`, with target/profile/model identity visible before action | READY requires `/health` and exact `/v1/models` served id |
| Stop/restart | Stop has unmistakable closure; restart uses the same saved profile | Owned process/container is gone after stop and exact identities match after restart |
| Reinstantiate | Human can select the saved profile after a fresh app/agent process | Second launch matches the first config hash, argv/env, image/build, model SHA, and served id |
| Failure cleanup | Error remains visible and app remains usable | Shielded cleanup runs after every post-launch failure; unrelated baseline is unchanged |

## Phase A — Establish an honest baseline

- Record branch/ref, worktree state, toolchain, daemon identity, oxcart GPU/container/port baseline, and the current remediation evidence.
- Run the full suite with the plan-mandated Homebrew Python 3.11; classify environment-only failures separately and do not call them product regressions.
- Reproduce the complete saved-profile journey in the visible TUI: create, pin, review, save, cold reload, select, preview.
- Retain before-state checks so final cleanup can prove no unrelated oxcart state changed.

**Gate A:** baseline evidence is complete and every newly observed defect has a failing acceptance test or an explicitly documented manual gate.

## Phase B — Saved Deployment Fidelity v1

Implement these release-critical invariants:

1. Process selectors persist immutable `build_id`, never a mutable label when an id exists.
2. Cache claims are revision-specific. A saved revision cannot borrow the cached state of a registry entry that was repinned to another commit.
3. Model Manager's temporary override is explicitly `Use once`, scoped to the selected profile, cleared on config change, and consumed after one launch attempt.
4. Clone delegates to the full-config clone primitive. Only the disclosed collision and ownership changes may differ: name, port/container identity, runs directory, the owned profile label, and removal of source-container eviction. Every other field is preserved.
5. Fresh-process round-trip coverage saves through a real local agent, constructs a new agent/app, reloads from disk, and compares normalized config plus resolved preview.

**Gate B:** strategic tests prove build-label drift, repin/cache drift, cross-config override leakage, repeat-override leakage, and lossy clone are impossible.

## Phase C — Guided composition and review honesty

1. Make recipe identity explicit in the compose request. `Custom` disables automatic recipe matching.
2. Switching from a recipe to Custom restores the pre-recipe draft rather than leaving invisible derived values.
3. Validate required Docker image on the Runtime step; helper copy must not claim that a preset supplies an image when it does not.
4. Review displays target, runtime identity, model repo/ref/full SHA, served id, bind/exposure, resolved argv, redacted env, cache/mounts, and any eviction/destructive actions with provenance.
5. Save conflicts retain the draft and return to an editable review/rename choice.
6. Rename lifecycle language from ambiguous `Load` to `Launch` wherever it starts compute.
7. Add `Ctrl+S` submit to Pin Model and keep its footer reachable in short viewports.

**Gate C:** headless screen tests at 80, 100, and 142 columns plus a visible recipe -> Custom -> review -> save-conflict recovery walkthrough.

## Phase D — First-class oxcart-local profile

- Add a checked-in oxcart recipe/profile derived from the currently installed Qwen3.6 27B FP8 stack, using:
  - target `local` while running on oxcart;
  - an exact vLLM image digest;
  - an exact HF model revision already present in oxcart's cache;
  - dedicated validation container, port, and runs directory;
  - the existing oxcart HF/vLLM/Triton/Torch/FlashInfer caches;
  - no broad eviction list and no shared-daemon restart.
- Surface the recipe in the wizard with human-readable source/provenance.
- Add static validation that rejects a tag-only image, floating model revision, baseline container name, or non-owned cleanup target.

**Gate D:** compose/preview tests pin the complete image, mounts, flags, container, port, runs directory, model SHA, and provenance.

## Phase E — Visible human pilot on oxcart

Run the controller and target locally on oxcart and expose only the controller UI to the Mac through a loopback SSH tunnel. Drive the actual UI in the visible browser:

The isolated controller must start with the real Oxcart cache as its own registry scan
authority (the container mounts alone do not affect controller-side pinning):

```bash
export HF_HOME=/tank/ai/models/qwen36-27b-fp8/hf-cache
export HF_HUB_CACHE=/tank/ai/models/qwen36-27b-fp8/hf-cache/hub
```

Record `huggingface_hub.constants.HF_HUB_CACHE` from that controller environment and
fail the pilot before opening the UI unless it equals the second path above. Do not set
controller-side `HF_HUB_OFFLINE`; the launched container remains offline through the
profile's own environment.

1. Open the dashboard and confirm target `local` means oxcart.
2. Create from the oxcart recipe; inspect every guided field.
3. Pin/select the exact cached model; inspect full SHA and cache state.
4. Review the complete resolved command and destructive-action summary.
5. Save without launching; record the YAML hash and preview.
6. Cold-restart the Vela app and its owned controller daemon; reselect the profile; compare hash and preview.
7. Launch to READY; verify `/health`, `/v1/models`, and a minimal inference request.
8. Stop through the UI; verify owned container/process and port are gone.
9. Cold-restart again, reselect, relaunch, and prove exact identity equality with the first launch.
10. Exercise one guided failure that is safe and reversible, then prove cleanup and UI responsiveness.

**Gate E:** all matrix rows have screenshots plus machine evidence. No claim of success is based only on a badge or screenshot.

## Phase F — Release gates and evidence retention

Run, in order:

```bash
/opt/homebrew/opt/python@3.11/libexec/bin/python3 -m ruff check .
/opt/homebrew/opt/python@3.11/libexec/bin/python3 -m mypy
/opt/homebrew/opt/python@3.11/libexec/bin/python3 -m pytest -q
```

Then run the repository remote-validation/manual lane at the exact branch SHA, with the owner-directed oxcart-local live proof recorded as an amendment to the original Blackbird wording. Retain:

- branch/ref and clean-tree proof;
- normalized config and SHA-256;
- resolved command and redacted environment digest;
- model registry entry and full commit SHA;
- image repo digest;
- `/health`, `/v1/models`, inference result, and phase timings;
- pre/post GPU, container, process, port, and daemon inventory;
- visible screenshots for create/review/saved/cold-reload/READY/STOPPED/reinstantiated;
- cleanup result and known limitations.

**Gate F / definition of done:** local quality gates are clean; all profile-fidelity tests are green; the visible oxcart-local journey passes twice across cold restarts; exact runtime/model identity matches; owned resources are removed; unrelated oxcart state is unchanged; the worktree contains only intentional reviewed changes and retained evidence.

## Deferred work

The original optional Phase 10 structural file splits remain deferred. They do not close a user workflow or fidelity gap and must not be interleaved with this completion effort.
