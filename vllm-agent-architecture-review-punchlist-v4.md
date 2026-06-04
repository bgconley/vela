# vLLM Agent Architecture — Implementation Review Punch List **v4**

**Supersedes (does not replace):** v1, v2, v3 punch lists. **v1 P1–P4, v2 P1–P6 + PA7, and v3 P2–P5 + cleanup + build-repair are all CLOSED** (verified).
**Reviewing:** `vllm-agent-architecture-spec-v1.md` (+ build/model sibling specs).
**State at this review (2026-06-04, HEAD `8bd9c59`):** **562 tests passing.** Architecture ~97% • features ~95% • validated ~78–80% • **overall ~92% to a shippable v1.**
**Method:** three Sonnet 4.6 reviewers; load-bearing/safety findings independently verified by Opus 4.8 (**[Opus-verified]**). *Note: the repo moved during review (coder is committing live, with in-flight edits to the validation script/docs — i.e. V4-P1 is already being worked).*

**Headline:** the architecture is done; this is the final ~8%, all validation + docs polish. Three items, shortest list yet.

---

## ✅ Closed since v3 (verified) — DO NOT REGRESS

- **FlagManager editing** — typed `Input` → live `on_input_changed` → async preview worker → `ctrl+s` persist. **[Opus-verified, code read directly]**
- **Structured DownloadModelScreen**; url-source download cleanly short-circuits as launch-time-only (no dead `feature-unavailable`).
- **Restart/preflight RPC wiring** — controller calls `restart` (with controller-minted `new_run_id`) and `preflight` directly.
- **Lock consistency** — `pin_model` nests entry+registry locks like other write paths.
- **Build repair** — `r` in BuildManager → agent `repair_build` → re-derive paths + regenerate `bin/`/`run.sh` without reinstall (build §7.6).
- **Cleanup** — legacy form parsers removed.
- **Safety invariants intact. [Opus-verified]** Crown-jewel clean; live-run remove guard intact (force can't bypass); installers real subprocess; scrub-before-wire; wire-safety round-trip.

Add a regression test before refactoring near any of the above.

---

## V4-P1 — Validation: self-hosted cadence + one real-model resume (the only carry-over) **[Opus-verified — CI yml + artifacts read directly]**

**Severity:** Medium (the code is ready; this is about keeping the *proof* current and covering one real surface).
**Carries from:** v3-P1.

**Blackwell addendum.** The earlier review under-weighted that this lab owns the
Blackwell. A self-hosted real-hardware lane is therefore a realistic target, not
a theoretical stretch. The right cadence is still **better proof cadence, not
gate every commit**: a 30-60 minute Qwen/build/model run is too heavy for every
push, and one shared GPU needs a concurrency guard plus trusted-code hygiene.

**What's true now (good).** `scripts/run_remote_tests.sh` is a genuinely repeatable lane that *executes* a real `vllm` build install + real HF download + real Qwen `smoke-tui` against the GPU host and writes dated, commit-pinned artifacts. Fresh P620 controller → Blackbird target artifacts exist at HEAD `b085610`: `artifacts/remote-validation/2026-06-04-p620-blackbird-b085610-build-model-resume.md` covers managed build install, tiny HF model pin/download, and real model resume/daemon restart; `artifacts/remote-validation/2026-06-04-p620-blackbird-b085610-qwen-smoke.md` covers Qwen3.6 27B FP8 `smoke-tui`. A `.github/workflows/remote-validation.yml` lane is wired for manual dispatch, nightly self-hosted execution, fast/full profiles, and concurrency guarding. **[Opus-verified]**

**What's not yet true.**
- **Workflow still needs live runner proof:** the YAML now targets self-hosted scheduled/manual real-hardware validation, but the actual GitHub Actions self-hosted runner/secret/concurrency path still needs a live run. The manual P620→Blackbird path is proven at HEAD.

**Important nuance (don't over-fix).** Auto-gating a real GPU run on every commit is **not realistic** for a lab tool (slow, costly, and contends with research use). The stronger target is scheduled/manual self-hosted proof with a concurrency guard, plus an optional `fast` profile for cheaper build/model validation.

**Fix direction.**
1. Stand up or point `remote-validation.yml` at a trusted self-hosted runner on Blackbird or P620 with LAN/SSH reach to Blackbird. Set `VLLM_LOADER_REMOTE_SSH_KEY` or equivalent runner credentials.
2. Keep manual dispatch, and run the nightly/scheduled `full` profile so HEAD stays continuously proven without blocking every commit.
3. Use the workflow concurrency group as the first guard against double-booking the GPU; keep the lane restricted to trusted branches/tags/manual runs, not untrusted fork PR code.
4. Keep the `fast` profile for cheaper build+tiny-model validation and reserve the full Qwen smoke + real resume for nightly/release or explicit dispatch. Use the small checked-in `tiny-random-llama-detached-blackbird` config for resume/restart; keep the heavyweight Qwen config as the TUI smoke.
5. Re-run the full real build+model+Qwen lane at HEAD and record a fresh artifact.
6. Run one resume + daemon-restart pass against a real model using `VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG`, not the fake child.

**Acceptance.** A dated artifact at (or within 1-2 commits of) HEAD covering real install + download + launch, **plus** one resume/daemon-restart record against a real model, produced by the self-hosted/manual or scheduled lane with concurrency in place.

---

## V4-P2 — Docs (the biggest "looks unfinished" surface for v1)

**Severity:** Medium.

**Problem.** `README.md` is ~59 lines; there's no config-schema reference, no build-method reference (pip/nightly/commit/wheel/git + the uv requirement), no targets/agent setup guide, no API/RPC overview. `docs/gpu-workflow.md` exists but is operational notes, and the punch-list/spec `.md`s are dev artifacts, not user docs.

**Fix direction.** A real README + a short docs set: quickstart (local + a remote target), the config schema (incl. `target`, `command.build/cwd`, `model_ref`/`revision`), the build-method matrix (and that nightly/commit need `uv`), targets/daemon setup (systemd + auto-spawn), and a one-page RPC/architecture overview. Pin the tested vLLM/host matrix.

**Acceptance.** A new contributor can install, register a target, create/select a build, pin/download a model, and launch — from the docs alone.

---

## V4-P3 — Build edges (minor)

**Severity:** Low.

- **Confirm `git`-source build has a `pip` fallback when `uv` is absent.** `nightly`/`commit` hard-requiring `uv` is **spec-correct** (pip can't honor index priority). But source builds should fall back to `pip` — verify `_git_install_request`'s non-uv branch (`agent/local.py:~3073`) does, or add it. **[Opus-flagged]**
- **Surface the `uv` requirement** for nightly/commit in the create-build UI/CLI (so the method silently failing with `feature-unavailable` becomes an upfront "requires uv on the target").
- *(Forward-compat)* the unrecognized-`source` model-download catch-all (`agent/local.py:~1914`) and unknown-`method` build catch-all (`~2917`) are fine as guards — leave as-is.

**Acceptance.** `git`-source build works (or fails clearly) without uv; nightly/commit surface the uv requirement before the job, not as a late `feature-unavailable`.

---

## Definition of done (v4)

**Done = V4-P1 (fresh at-HEAD artifact + one real-model resume) + V4-P2 (real docs).** V4-P3 is minor polish. At that point the architecture is built, proven current against real hardware, and documented — a shippable v1. The remaining work is **validation hygiene and writing, not engineering.**
