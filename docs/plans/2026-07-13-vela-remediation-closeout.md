# Vela Remediation Closeout Plan

**Date:** 2026-07-13

**Branch:** `remediate/2026-07-09-review`

**Parent authority:** `docs/plans/2026-07-09-vela-remediation.md`
**Purpose:** Close every verified implementation, evidence, bookkeeping, runtime, and
remote-validation gap that prevents the parent plan's Phases 1-9 and whole-plan definition of
done from being true.

## 1. Completion rule

This closeout is complete only when the checked-in code, live UI, local runtime state, README
quickstarts, repository bookkeeping, and Blackbird validation all agree. A green local test suite
is necessary but not sufficient.

Phase 10 remains out of scope. The parent plan says structural splits happen after Phases 1-9 are
merged; starting them now would mix low-value mechanical churn into release-proof work.

## 2. Baseline and verified gaps

Baseline at closeout start:

- HEAD `a7e67b7` on `remediate/2026-07-09-review`.
- Worktree clean.
- Fresh audit reproduced 1,417 passing tests, clean Ruff, clean mypy across 74 source files, and
  an unchanged 11-module mypy override ratchet.
- Exactly 23 ignored final walkthrough screenshots existed, but they had no durable manifest or
  checksums.
- The real Mac daemon was PID 66788, started 2026-06-09, and deliberately left untouched during
  the audit.
- Two orphaned `scripts/fake_vllm_child.py` listeners remained: PID 48705 on port 63005 and PID
  48838 on port 63145.
- GitHub Actions scheduled run 29188445178 was queued from `main`; the branch workflow was
  dispatch-only, but the default branch still scheduled runs.

Gap matrix:

| Authority | Gap | Required closeout |
|---|---|---|
| Phase 3.5 / DoD 2 | bug-239 still says `fix: pending` | Re-verify busy/error paths, write real fix text, and retain bug identity |
| Phase 4.8 | Checkbox states are colored blocks/Textual `X`, not `[ ]` / `[✓]` | Implement exact glyph grammar and pin it with rendered-state tests |
| Phase 4.8 | `target_edit.py` still imports legacy `TEXT` | Migrate to `TEXT_PRIMARY` and structurally pin the canonical token |
| Phase 4.11 | `after-phase4` evidence omits required screens | Re-run every required screen and width in the final closeout walkthrough |
| Phase 5.3 | Explicit user `docker.hf_cache` is discarded | Preserve the explicit value; default only when absent; test all precedence branches |
| Phase 6.6 | June daemon housekeeping not executed | Safely stop/restart only the local Mac daemon and prove new revision identity |
| Phase 7.2 | `vela runs list` omits `started` | Add a controller-safe start timestamp without leaking PID/path data |
| Phase 8 / DoD 4 | Installed-tool quickstart not proven fresh | Run both README paths in isolated homes/config/runtime directories exactly as written |
| Phase 9.4 | Schedule still operational on `main`; queued run remains | Cancel stale scheduled run; publish/merge workflow removal before relying on dispatch-only behavior |
| Phase 9.5 | Anatomy counts ignored/untracked files as tracked | Rescan and verify the generated inventory against `git ls-files` |
| DoD 3 | Final visuals are ignored and lack provenance | Capture fresh after-shots plus an evidence manifest and SHA-256 checksums |
| DoD 5 | No green remediation-branch Blackbird lane | Run `workflow_dispatch` or the manual runbook safely, retain artifact and run URL |
| Cleanup claim | Two fake-child listeners remain | Verify identity/cwd/ports, terminate only those two, and prove ports/processes are gone |

## 3. Execution rules

1. Use strict red-green TDD for every code correction: add the failing contract test, run it and
   record the expected failure, make the minimal implementation, then run the focused green gate.
2. Preserve TUI widget IDs, dismiss payloads, and pinned substrings unless the parent plan
   explicitly changes them.
3. Run with Homebrew Python, not the repository `.venv`.
4. Keep all local/browser test instances under short, isolated `/tmp` XDG and agent-runtime paths.
5. Never stop the shared Blackbird daemon. A remote validation may update the editable checkout
   and run the documented test/smoke flow, but cleanup must leave containers/GPU state idle and
   restore shared hosts to their prior branch when the runbook requires it.
6. Do not rewrite Git history. Commit identity cleanup remains an owner decision under parent
   decision D6.
7. Do not claim a gate from screenshots alone. Pair visual evidence with code/test/runtime proof.

## 4. Workstream A - Phase 4 exact UI contracts

### A1. Checkbox grammar

- Add a failing headless test that inspects the rendered toggle button for unchecked `[ ]` and
  checked `[✓]`, including dim/green style roles.
- Implement a reusable Checkbox subclass or Textual-compatible render override if CSS alone
  cannot change the glyph. Keep existing checkbox IDs and values unchanged.
- Exercise both required surfaces: New Deployment `Download now` and Flag Manager `Changed only`.
- Render both states in the live browser at 80 and 142 columns.

### A2. Target Edit token migration

- Add a structural failing test proving `target_edit` no longer binds legacy `TEXT`.
- Replace it with `TEXT_PRIMARY`; retain `BAD` only where the intentionally different destructive
  color remains the app-wide authority.

### A3. Phase 4 evidence completion

Capture dashboard idle/running/stopped, Target/Model/Build/Flag managers, wizard, Help, Config
Picker, and responsive dashboards at 80, 100, and 142 columns. Verify no clipping, collisions,
or orphaned glyph/label pairs.

## 5. Workstream B - Phase 5 HF-cache precedence

Precedence contract:

1. Explicit user `docker.hf_cache` wins.
2. Otherwise a matched recipe's `docker.hf_cache` is preserved.
3. Otherwise generic Docker plus an `hf_repo` model gets the agent-resolved default.
4. Local-path and URL models remain unaffected.

Add strategic tests for each branch, including a sentinel explicit path that fails against the
current implementation. Ensure preview/preflight consumes the same resolved mount.

## 6. Workstream C - Phase 7 run start time

- Identify the agent-side canonical timestamp without sending a PID, raw sidecar path, or process
  handle to the controller.
- Return a normalized ISO-8601 UTC `started` value on the already-scrubbed status surface.
- Render it in text and JSON `vela runs list` output.
- Preserve the single-connect discover/status sweep and all bug-225 leakage assertions.
- Cover process and Docker runs, including the honest unknown fallback.

## 7. Workstream D - bookkeeping, runtime, and repository truth

### D1. Buglog and cerebrum

- Replace bug-239's pending text with the actual busy/error implementation and verification.
- Record the three closeout corrections under new real bug IDs without duplicating auto-hook junk.
- Add durable gotchas: explicit-value precedence must be tested; a plan-required output column
  cannot be silently dropped; visual glyph contracts require rendered assertions.

### D2. Local process hygiene

- Reconfirm PIDs 48705/48838 command line, PPID, cwd, and listening ports.
- Gracefully terminate, then force only if still alive; verify both ports are free.
- Stop and restart the real local daemon through `vela agent restart`, verify its new PID/start
  time/socket/revision, and do not disturb unrelated processes.

### D3. CI schedule and anatomy

- Cancel queued scheduled run 29188445178 if still queued.
- Keep branch workflow dispatch-only and prove the default branch no longer schedules after the
  change is published.
- Run `openwolf scan`; independently reconcile its tracked count and entries against
  `git ls-files`, excluding ignored/untracked `.DS_Store`, `.firecrawl`, and local settings.

## 8. Workstream E - hermetic quickstarts

Use separate temporary homes and XDG/runtime roots. Do not reuse an editable installation from the
developer checkout as proof of the installed-tool path.

### E1. Installed tool

- Install from the exact branch/ref being certified using the README's `uv tool` route.
- Run `vela --version`, `vela --help`, and open/exit the first-run TUI.
- Prove the installed package contains no bundled configs and displays the intended TUI-first
  guidance.
- After the branch is published at the README URL, repeat the literal README command.

### E2. Cloned repository

- Clone/copy into a clean temporary directory, install `.[dev]`, and run the commands exactly as
  documented: `vela list`, `vela run fake-child --preview`, and `vela smoke fake-child`.
- Prove READY, successful auto-stop, no listeners/processes left, and exit code zero.

## 9. Workstream F - quality and visual gates

Run, in order:

```bash
python3 -m ruff check .
python3 -m mypy
python3 scripts/check_mypy_overrides.py
python3 -m pytest -q
```

Then run the full live walkthrough in an isolated browser-served TUI:

1. Pristine first-run and Help open/close.
2. Full New Deployment wizard, including required-field errors and pin-handoff cancel/success
   round trips.
3. Review command with immutable pin SHA and field provenance.
4. Config Picker filter/no-match/scroll behavior.
5. All four managers and exact checkbox states.
6. Fake-child launch, phase timing, READY, stop toast, terminal phase, and operator closure.
7. Dead SSH target connect, responsive interaction, cancellation back to local, and bug-307
   attribution behavior after its disposition.
8. Dashboards at 80, 100, and 142 columns.

Save screenshots and a machine-readable manifest under
`artifacts/remediation-closeout/2026-07-13/`. The manifest must identify commit, branch, test
commands/results, viewport/cell sizes, isolated runtime roots, screenshot filenames, and whether
each acceptance criterion passed. Add SHA-256 checksums for every retained artifact.

## 10. Workstream G - Blackbird release proof

Only after local gates are green:

1. Verify GitHub variables/runner availability and the two-host checkout/venv paths.
2. Commit and publish an immutable `certified_code_sha`; pass its branch and full SHA through the
   workflow/manual lane and require `REMOTE_REVISION_OK expected=<sha> actual=<sha>` before install.
3. Prefer the non-disruptive fast/manual lane on the P620 controller with `--target blackbird`.
   It must use a validation-only outer daemon runtime and may not restart the shared Blackbird
   daemon. A Qwen/full smoke is an owner-scheduled maintenance exercise, not an automatic closeout
   action.
4. Monitor through completion; do not equate a queued/cancelled run with proof. If the self-hosted
   runner is unavailable, use the parent plan's explicit manual `run_remote_tests.sh` alternative.
5. Require green remote tests, previews, isolated-daemon survival, Blackbird target handshake, and
   requested non-destructive build/model evidence.
6. Verify no leftover validation container, model process, occupied port, or GPU allocation. The
   real-resume helper must best-effort stop its owned run on every failure path.
7. Retain the remote artifact and checksum outside the certified code commit. If a later
   evidence-only commit records its path/result, state explicitly that its parent is the remotely
   certified code SHA; do not relabel that evidence commit as remotely certified.

## 11. Final adversarial audit

Re-read every parent-plan phase gate and the five whole-plan DoD clauses against the resulting
tree and live evidence. The final report must classify every item as validated or blocked and must
not upgrade a local/offline result into remote/release proof.

Completion requires all of the following:

- [ ] Phase 4 exact contracts and complete visual set pass.
- [ ] Phase 5 explicit HF-cache precedence passes.
- [ ] Phase 7 `started` output passes without wire leakage.
- [ ] Bugs 233-240 all have real fix text; anatomy is truthful.
- [ ] Ruff, mypy, override ratchet, and full pytest pass fresh.
- [ ] Both fresh-machine quickstarts pass as written.
- [ ] Orphan fake children are gone and the local daemon runs current code.
- [ ] Dispatch-only CI behavior is active on the default branch.
- [ ] A green Blackbird run certifies the exact final SHA.
- [ ] Durable evidence manifest and checksums validate.
