# Vela remediation and Oxcart human-workflow closeout

## Outcome

The owner-amended completion effort **passes** at runtime source
`cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7`: Oxcart acted as both controller and
target through Vela's `local` transport, the complete human workflow was driven in
the visible UI, the same immutable deployment was launched twice across cold
controller/agent restarts, real text and image requests passed, and every run-owned
resource was removed.

This is a **branch-complete, not tag-ready** verdict. The parent plan's literal
Blackbird-before-tag, merge-to-main, unqualified-main quickstarts, and tag remain
external release actions. Optional Phase 10 structural splits remain deferred by
design.

Evidence verdict: `pass_with_live_findings_fixed`.

## Authority and topology

- Parent authority: `docs/plans/2026-07-09-vela-remediation.md`.
- Completion authority: `docs/plans/2026-07-13-vela-human-workflow-completion.md`.
- Owner amendment: Oxcart was controller and target; `local` means Oxcart in this
  lane. No shared Oxcart daemon was restarted.
- Runtime source: exact detached worktree at `cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7`,
  which was also the published branch SHA during both launches.
- UI surface: visible in-app Chromium connected through a loopback-only SSH tunnel
  to `textual-serve` on Oxcart.
- Model endpoint: `http://127.0.0.1:18004` on Oxcart.

## Claimed completion: validation verdicts

| Claim from the prior agent | Verdict against plan and code | Current resolution |
|---|---|---|
| Phases 1–9 implemented | Substantially true for implementation, but not sufficient to prove the literal whole-plan release DoD. | Phase implementations now pass current-tree gates and the owner-amended Oxcart proof. |
| Whole-plan definition of done met | False as originally stated: the parent plan still required two fresh quickstarts and a Blackbird lane before tag, and the supplied browser walk did not prove real deployment fidelity. | Exact-SHA installed-tool and cloned-repo quickstarts now pass; Oxcart owner-amended lane passes; literal Blackbird/merge/main/tag remain external. |
| Branch was approximately 120 commits ahead | False. The runtime branch was 101 commits ahead of `origin/main`; the pasted accounting itself reported 96 at its earlier SHA. | Use Git, not an approximation, for release accounting. |
| Suite was 1417 tests and clean | Historically plausible but stale, not current proof. | Fresh final tree: **1569 passed in 290.92s**, Ruff clean, mypy clean in 75 source files, override ratchet unchanged at 11. |
| All walkthrough processes cleaned | False historically: an old Oxcart `textual-serve` pipeline remained on 8815, and a separate stale Mac fake supervisor/child remained on 60821. | Both identities were checked and removed without touching real daemons; final Oxcart root/worktree, ports, container, processes, and Mac tunnel are absent. |
| Bugs 233–240 no longer reproduced | Supported by current walkthrough and tests. | Superseded by 67 current captures plus machine evidence at the exact runtime SHA. |
| bug-307 was cosmetic | False. It was a real target-switch/generation ownership race capable of attributing a stale error to the new target. | Fixed with generation plus client-identity ownership and deterministic regressions before this pilot. |
| The Mac still ran a June 9 daemon plus PID 20556 | False at current audit time. The real daemon was a July 13 process at `de9b0a1`; PID 20556 was absent and all original Phase 6–8 commits were ancestors of its revision. | The shared Mac daemon was not a blocker or part of the Oxcart proof. A separate stale test pair was safely removed. |
| All commits use the hostname-derived author email | True for the 101 runtime commits. | Identity rewriting remains an owner choice, not a functional release gate. |
| Phase 10 is optional | True. | Deferred; no workflow or fidelity gap depends on it. |

## Completion gates

| Gate | Result | Proof |
|---|---|---|
| A — honest baseline | Pass | exact source, clean remote worktree, GPU/container/port guards, first-run and failure baselines |
| B — profile fidelity | Pass | build/model identity, use-once scoping, clone fidelity, and fresh-agent round-trip tests |
| C — guided composition | Pass | visible recipe → Custom restoration, immutable runtime/model guidance, complete Review, save-conflict recovery, width tests |
| D — Oxcart profile | Pass | retained `saved-profile.yaml`, exact digest/SHA/cache/mount/port/run-dir identity, static validation |
| E — visible Oxcart pilot | Pass | 67 captures, two real runs, endpoint probes, cold restart equality, negative paths, final cleanup |
| F — release evidence | Pass for owner-amended branch completion | local gates, exact-SHA Oxcart remote lane, quickstarts, secret scan/checksums, cleanup; literal external release actions remain |

## Human journey and machine-proof crosswalk

| Journey | Visible result | Machine proof |
|---|---|---|
| First run | Honest empty state and quick-start guidance | installed-tool PTY first-run, no registry crash |
| Create deployment | Guided recipe and understandable inputs | Review payload and profile schema tests |
| Recipe → Custom | Draft visibly restored | recipe/custom state tests; no hidden derived launch values |
| Pin model | Full immutable SHA and cache state visible | model entry `01KXDPJ7NNB825TG9R2WANB0MS`, commit `e89b16e…eb09` |
| Runtime | Docker digest visible | retained profile uses `vllm/vllm-openai@sha256:b13d6e…0046` |
| Review/save | Full provenance, redaction, mounts, flags, command; Save distinct from Launch | saved YAML hash `82584154…c6a5`, preview hash `14c3d7cb…8267`, no container/18004 after save |
| Config picker | Current profile marked and selectable | `test_config_picker_preselects_and_marks_current_config` |
| Use model once | Action and affected profile visible | `test_model_use_once_is_visible_and_never_leaks_across_config_switch`, `test_model_use_once_is_consumed_once_but_shared_by_entire_launch_attempt`, real-agent restoration test |
| Clone | Regenerated fields disclosed; clone selected | three maximal process/Docker/ownership clone tests plus palette clone fidelity test |
| Save conflict | Error visible; draft editable; rename and save recovered | `test_save_name_conflict_restores_the_human_draft_for_rename` and full flag-survival conflict test |
| Cold restart | Original profile reselected twice | YAML, preview, command hash, model/runtime identity equal |
| Launch | READY shown with target/profile/model/endpoint | `/health` 200, sole `/v1/models` id, exact text and vision results |
| Stop/reinstantiate | Clean STOPPED closure, then same profile relaunched | both terminal records `STOPPED`, intentional, rc 0; normalized identities equal |
| Safe failure | Wrong-host error and dead-SSH connecting state remained responsive | no run artifact/container/18004 from wrong-host; dead-target return-to-local matrix |
| Widths/managers | Target/Model/Build/Flag and 800/1000/1420-pixel column-equivalent views usable | parametrized 80/100/142 terminal tests and layout regressions |

The visible Use-once action was applied, but one-attempt consumption and cross-profile
clearing are machine-tested rather than proven with another real GPU launch. The
width captures are column-equivalent viewport widths, not direct terminal-cell
measurements.

## Real Oxcart runs

| Property | Run 1 | Run 2 |
|---|---|---|
| Run id | `7489df3a708842428332de3e984a012d` | `7da79f87c9b74c26b8961360815ef63f` |
| Health | 200 | 200 |
| Sole model id | `qwen36-27b-fp8-oxcart` | same |
| Text | `VELA_TEXT_OK` | same |
| Vision | `LEFT=RED; RIGHT=GREEN` | same |
| Terminal | intentional `STOPPED`, rc 0 | intentional `STOPPED`, rc 0 |
| Post-stop | container absent, 18004 absent, GPU 2 MiB / 0% | same |

The vision input was a real 64×32 RGB PNG with a red left half and green right half,
SHA-256 `21ab0d7b6f3967f4f2f6baccbdafdaeee667b97d8dfcc027b71b0fa6a9af8c57`.

Repeatability is exact after normalization: saved profile, preview, resolved command
hash, argv/environment, image digest, model entry/repo/revision/commit, flags, mounts,
served id, and endpoint assertions match. Only the run id and Docker container id
differ. Run 1 retained an independent Docker-inspect snapshot; Run 2 identity is
retained through its sidecar/artifact and normalized comparison, an acknowledged
evidence asymmetry.

Backend evidence proves Cutlass FP8 plus FlashInfer attention and no Marlin fallback.
`FLASHINFER_MOE` was neither applicable nor observed because this is a dense model.

`phase-timings.json` retains engine milestones and reported durations. Run 1 reported
74.535 s model loading and 94.49 s engine initialization; warm-cache Run 2 reported
7.206 s and 21.91 s. The first retained health-probe time is explicitly an upper
bound on READY, not the readiness-transition timestamp.

## Findings discovered and fixed during the live pilot

1. **bug-327 — runtime Review crash.** Bracket-rich Docker/provenance data crossed
   Textual markup-aware boundaries as raw strings. Dynamic `Static` content now uses
   `markup=False`; `Select` labels use literal `rich.text.Text`, retaining type-to-search.
2. **bug-328 — wrong attached-run authority.** `vela runs list` intentionally reports
   detached runs and returned empty for the attached TUI launch. The runbook now scans
   only the exact configured runs directory, requires the expected profile/attached
   mode/no exit artifact, verifies the sidecar against the system, and fails unless
   exactly one candidate remains. Assignment and `export` are separate so shell
   failures cannot be masked.
3. **bug-329 — visible-server ownership.** A `textual-serve | tee` pipeline could
   orphan a server/UI child. The runbook now uses `exec`, retains PID/create-time/cwd/
   executable/full-command identity, requires browser disconnect and child absence,
   signals only the exact identity, and gates deletion on process and listener absence.

## Remote validation and cleanup

The current-SHA Oxcart safe remote lane passed Ruff, 165 focused tests, daemon-restart
live-run survival, and disconnect/reconnect stream resume. The first attempt retained
only `start-failed` under the deliberately long AF_UNIX socket path; path length is an
inference because its raw `agent-start.err` was not retained. Re-running through short
`/tmp/vela-agent-cd9569a` succeeded and then cleaned its processes, sockets, listeners,
and temporary worktree.

Before deleting the main validation root, every local evidence file verified against
provisional checksums; the saved YAML was copied and independently hashed. Exact remote
preflight proved source clean, all known UI identities absent, no Textual PID files,
no owned daemon/container/GPU process, and ports 18004/8815/60867/50979 absent. The
registered worktree and exact run root were then removed. The Mac tunnel PID 39506 was
identity-checked, terminated, and local 8815 plus its exec session closed.

## Evidence limitations retained honestly

- Save, Run 1, and Run 2 Textual servers have process-absence proof but only partial
  historical create-time identity retention. Wrong-host and matrix sessions use the
  hardened full-identity procedure. The pack does not claim complete historical UI
  lifecycle provenance.
- `25a-run1-stop-check.jpg` is the clean visible Run 1 stop proof.
  `31a-run2-stop-check.jpg` contains the Run 2 STOPPED line but is render-degraded;
  the Run 2 machine terminal record is authoritative. Files `25-run1-stopping.jpg`
  and `31-run2-stopped.jpg` still show READY-like content and are not cited as STOPPED.
- Both vLLM logs end with an NCCL `destroy_process_group()` warning. All shutdown and
  residue gates pass, so the result is clean-with-runtime-warning, not warning-free.
- `runs-run1.json` is empty by attached-run design and is not used as launch proof.
- `wrong-host-listeners-after.txt` includes 8815 because the visible UI was still
  serving; final listener absence comes from hardened stop/final cleanup proof.
- Quickstart records are checksum-backed human-authored summaries, not retained raw
  terminal transcripts.
- The current OpenWolf buglog is valid and has unique ids, but OpenWolf 1.0.4's
  automatic hook chooses `bugs.length + 1`; historical numbering gaps mean its next
  automatic entry could collide with bug-317. No automatic entry was used during
  closeout. Treat allocator repair as OpenWolf tooling debt, not application release
  proof.

## Parent-plan reconciliation

| Phase | Final status |
|---|---|
| 1 — stability | complete |
| 2 — wizard state machine | complete |
| 3 — RPC feedback | complete |
| 4 — layout system | complete |
| 5 — model lifecycle | complete |
| 6 — daemon/discovery | complete |
| 7 — CLI friendliness | complete |
| 8 — docs/README | complete, including corrected Oxcart proof authority/lifecycle |
| 9 — repo diet/OpenWolf | complete after final excluded anatomy rescan |
| 10 — structural splits | optional, intentionally deferred |

The literal release sequence remains:

1. owner confirms whether the Oxcart amendment permanently replaces or merely
   supplements the parent plan's Blackbird-before-tag wording;
2. merge the reviewed branch to `main`;
3. run both quickstarts from unqualified `main`;
4. perform any still-required literal Blackbird lane;
5. tag only after those external gates pass.
