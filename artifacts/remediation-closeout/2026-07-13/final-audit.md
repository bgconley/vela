# Final adversarial audit

- Authority: `docs/plans/2026-07-09-vela-remediation.md`
- Audited code: `de9b0a13f2ef7166014794bb211845dd3ba96123`
- Branch: `remediate/2026-07-09-review`

## Outcome

The branch implementation is substantially complete and the remediation defects are closed in
the code, but the repository is not yet tag-ready under the parent plan's literal definition of
done. Three release gates require external state or owner authority: a live two-host Blackbird
smoke in a maintenance window, merging the certified branch to `main`, and then repeating the two
literal default-branch quickstarts plus confirming the default workflow no longer schedules.

Optional Phase 10 is not a gap. The parent plan explicitly defers it until Phases 1-9 are merged.

## Original coder claims

| Claim | Audit at start | State after closeout execution |
|---|---|---|
| All walkthrough processes cleaned up | Invalid: two fake children and stale walkthrough servers remained | Closed for owned Vela processes; unrelated `pi` processes were preserved |
| All nine phases and whole-plan DoD complete | Invalid: Phase 4, 5, 7, runtime, quickstart, evidence, CI, and remote gaps remained | Branch implementation gaps closed; external release gates remain below |
| Branch at approximately 120 commits | Invalid: 96 commits then, 97 at certified code SHA | Correct count recorded in manifest |
| Suite 1417, Ruff and mypy clean | Valid as a historical gate, but insufficient for completion | Fresh suite is 1437 green; Ruff, mypy, and override ratchet green |
| Twenty-three final shots prove zero defects | Incomplete: ignored files lacked manifest/checksums and missed required screens | Replaced by 32 retained, checksummed live browser captures |
| bug-307 was merely cosmetic | Invalid: stale target-switch/keepalive completions could mutate the new target | Fixed with generation plus client-identity ownership and deterministic tests |
| Both quickstarts complete | Invalid: only the cloned-repo path had been proven | Both exact-SHA hermetic lanes pass; literal `main` routes await merge |
| Remote lane complete | Invalid: it had not run | Exact-SHA safe harness and Blackbird handshake pass; live model smoke still required |
| Memory updated | Incomplete after the later closeout fixes | Local ignored `.wolf/memory.md` now records every closeout lane |

## Phase classification

| Phase | Classification | Evidence |
|---|---|---|
| 1 — stability | Validated | Full suite plus target-switch/keepalive adversarial regressions |
| 2 — wizard state | Validated | Live handoff cancel/no-refire, pin selection, review, and save |
| 3 — RPC feedback | Validated | Busy/error tests and responsive dead-target walkthrough |
| 4 — layout system | Validated | Exact checkbox/token contracts and 142/100/80-column captures |
| 5 — model lifecycle | Validated in code | Explicit HF-cache precedence and the complete phase regression suite |
| 6 — daemon/discovery | Validated locally | Current-revision daemon on the explicit socket; old PID gone |
| 7 — CLI friendliness | Validated | Safe ISO UTC `started` output and leakage regressions |
| 8 — docs/quickstarts | Branch validated; merge-dependent literal gate | Both exact-SHA hermetic lanes pass; unqualified URLs still resolve old `main` |
| 9 — repo diet/CI | Branch validated; merge-dependent activation | Queue cancelled, branch dispatch-only, anatomy truthful; `main` still has cron |
| 10 — structural splits | Optional/deferred | Parent plan says schedule only after Phases 1-9 merge |

## Whole-plan definition of done

1. **Validated.** Ruff clean, mypy clean across 75 files, 11-module override ratchet unchanged,
   and full pytest `1437 passed`.
2. **Validated.** Bugs 233-240 have real fix text, closeout gotchas are in cerebrum, the ignored
   local memory has a complete closeout summary, and anatomy rescans to 215 files with zero
   hits/misses while excluding the generated evidence root.
3. **Validated.** The full live walkthrough is retained with 32 after-shots, a manifest, and
   checksums. Cancel/no-refire and successful Pin-HF round trips both ran live; the success path
   used a pristine detached exact-SHA worktree whose daemon reported `v0.1.0-108-gde9b0a1`.
   None of bugs 233-240 reproduces; bug-307 was fixed rather than waived.
4. **Merge-dependent.** Both fresh hermetic lanes pass at the exact published SHA, but the
   README commands are intentionally unqualified and still resolve `main` at
   `88d18d897aabe87184a10575bcf8b52842ff20af`.
5. **Owner-scheduled live gate.** The exact-SHA harness ran safely on oxcart, 163 tests and two
   fake lifecycle probes passed, and the supported Blackbird handshake passed without changing
   its daemon, registry, workload, or GPU state. It did not run the parent plan's live Docker
   `smoke-tui` on Blackbird: an existing workload occupied 94308/97887 MiB. This is useful partial
   remote proof, not the required full Blackbird release proof.

## Required closeout sequence

1. Clear or schedule around Blackbird's existing workload, then run the documented two-host live
   smoke at exact SHA and retain READY/autostop/backend/residue/GPU-free evidence.
2. Decide whether the hostname-derived author email should be rewritten; D6 leaves this to the
   owner and it is not a functional gate.
3. Merge `remediate/2026-07-09-review` to `main`.
4. Run both literal README quickstarts from unqualified `main` URLs in fresh isolation.
5. Confirm `main` is dispatch-only and that no new scheduled remote-validation run appears.
6. Tag only after steps 1 and 3-5 pass. Consider optional Phase 10 afterward as a separate plan.
