# Session context — 2026-06-10 — journey punchlist done, GPU validation in flight

**Purpose:** resume the live-GPU validation cold after a /clear. Everything else
(code, punchlist, reviews) is committed; ONLY this debugging state was
conversation-local.

## Where things stand

- Branch `claude-ui-implementation`, pushed through `604520d`:
  - `3f7df48` — journey punchlist phases A-G complete (all 37 items, J1-J37)
    + the functional pass; reviewed by a 2-agent pass (zero contract drift,
    3 majors fixed pre-commit). Full LOCAL suite 1087 green, ruff clean.
  - `604520d` — test hermeticity: session fixture now isolates
    XDG_CONFIG_HOME too; 2 banner tests got the load_targets_file
    monkeypatch pattern (they silently depended on the dev Mac's
    targets.yaml defining `blackbird`).
- Punchlist: `vela-tui-journey-friction-punchlist-v1.md` — header says ALL 37
  ITEMS COMPLETE; §6 DoD met except the remote leg below.

## Live GPU validation (blackbird) — two rounds so far

Invocation that works (run from the repo root on the Mac):

```bash
VELA_SSH_OPTS="-i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519" \
VELA_REMOTE_BRANCH=claude-ui-implementation \
VELA_REMOTE_TARGET=blackbird \
VELA_REMOTE_TIMEOUT=2700 \
VELA_REMOTE_VENV=/home/bgconley/venvs/vela \
VELA_REMOTE_ARTIFACT=1 \
scripts/run_remote_tests.sh bgconley@10.25.0.51 /home/bgconley/repos/vela \
  qwen36-27b-bf16-rp6000-blackbird
```

Host facts (differ from the Jun-7 `.50` artifacts!):
- Host: `bgconley@10.25.0.51` (hostname `blackbird`), RTX PRO 6000 Blackwell
  Max-Q, 97.9 GB — NVML works, detected in round 1.
- Repo clone: `/home/bgconley/repos/vela` (NOT `~/repos/lab-tui` — that was
  the `.50` host). Venv: `/home/bgconley/venvs/vela` (no /tank/venvs here).
- `~/.config/vela/targets.yaml` on the host was MISSING; provisioned
  (user-authorized) with a local-transport `blackbird` entry — required by
  the script's VELA_REMOTE_TARGET phases.
- `run_remote_tests.sh` gained `VELA_REMOTE_BRANCH` (committed; conditional
  env injection so default stays byte-identical; tests updated).
- Both sides run Textual 8.2.7 — version skew RULED OUT.

Round 1: 4 failed / 1083 passed → 2 banner-test hermeticity failures (fixed
in 604520d) + the 2 below. Round 2 (with fix + provisioned target):
**2 failed / 1085 passed**, script exits before the preview/smoke phases:

- `tests/test_tui_smoke.py::test_new_deployment_create_build_handoff_pins_created_build`
- `tests/test_tui_smoke.py::test_new_deployment_build_pin_and_smoke_acceptance_flow`
- Error (both): `NoMatches: No nodes match '#label' on SelectCurrent(classes='-has-value')`

## Diagnosis (high confidence) + next step

Same family as bug-207/bug-208 (.wolf/buglog.json): the tests open the wizard
(`pilot.press("n")` → ONE `pilot.pause()`) then immediately assign
`Select.value` (runtime / model-mode). On the slower GPU box the Select's
INTERNAL `SelectCurrent#label` child has not composed yet → assigning value
makes Textual update SelectCurrent → NoMatches. Passes on the faster Mac.
The wizard got heavier this session (disclosure groups etc.), newly exposing it.

**Fix:** in BOTH tests, after opening the wizard, replace the bare pause with
a readiness wait before any `Select.value` assignment, e.g.:

```python
await _wait_for_condition(
    lambda: app.screen.id == "new-deployment"
    and bool(app.screen.query("#new-deployment-runtime SelectCurrent #label")),
    "wizard selects not ready",
)
```

(Any equivalent that proves the Select internals are composed is fine. Cannot
be reproduced locally — verify by rerunning the remote invocation above.)

## Remaining checklist

1. Fix the 2 tests' waits → local suite green → commit + push the branch.
2. Rerun the invocation above. Expect after pytest: `vela preview
   qwen36-27b-bf16-rp6000-blackbird` then a timeout-bound LIVE `smoke-tui`
   launch on the Blackwell (READY → auto-stop), then target-backed
   build/model checks, then a dated artifact under
   `artifacts/remote-validation/` (committed by the script? No — written
   locally; commit it).
3. If smoke fails: error banners carry kind+guidance; the config is
   `qwen36-27b-bf16-rp6000-blackbird` (exists on host in both
   `~/.config/vela/configs/` and the repo's `configs/`).
4. Wrap-up: log results to .wolf/memory.md, update the punchlist DoD line,
   final report. Optional next: merge/PR decision for
   `claude-ui-implementation` (user's call), Figma as-built mocks for E/F/G.

## Monitor pattern that worked

```bash
# run_in_background the script, then:
tail -f <task-output-file> | grep -E --line-buffered \
  "^== |passed|failed|READY|SMOKE|VELA_SMOKE|error:|fatal:|Traceback"
```
