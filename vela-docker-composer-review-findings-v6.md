# Vela — Docker Runtime + Deployment Composer Review (Findings v6) — 2026-06-06

**Method (this round, round 6):** re-established git/test ground truth → 7 **Sonnet 4.6** finders (one per domain, evidence-based, file:line anchors) → **Opus 4.8** independently re-read every cited location, rendered confirmed/refuted/adjusted verdicts + a completeness pass per domain → **Opus 4.8** synthesis (this doc) with independent corroboration of the load-bearing claims.
**Scope:** the 5 docker-integration spec docs — `vela-docker-runtime-spec-v1.md`, `vela-docker-runtime-examples-v1.md`, `vela-deployment-composer-spec-v1.md`, `vela-deployment-composer-user-stories-v1.md`, `vela-deployment-composer-implementation-plan-v1.md`.
**Ground truth:** HEAD `4d2fdca`; **23 commits** since the v5 snapshot baseline `2542867`; **803 tests pass deterministically** (107.86s, `-p no:randomly`); **ruff clean**; **crown-jewel grep clean**. Working tree is now **clean of uncommitted code** (only `.wolf/*` bookkeeping + review docs untracked).
**Workflow stats:** 14 agents, 1.64M subagent tokens, 846 tool calls, ~17 min. 121 verified findings (12 high / 18 medium / 33 low / 58 info); verdicts: 111 confirmed, 9 adjusted, 1 refuted. + 24 Opus-caught missed findings (3 medium / 14 low / 7 info).

---

## 0. Headline

**The architecture gap that defined the v5 snapshot is closed.** At v5, DK1–DK4 were "NOT YET — the generator exists but nothing launches/monitors/stops a container natively." As of HEAD `4d2fdca`, the **full native-docker lifecycle exists, is identity-safe, and is validated on real Blackbird hardware** (FP8 + BF16, both reached READY via `runtime: docker`).

**Completion: ~85% to a polished, fully spec-compliant v1 of the DK+DC feature set.** The functional MVP path — compose → save → native-docker launch → READY → safe stop — is **complete and hardware-validated (~95% on the core capability)**. The remaining ~15% is a tail of operability (DockerErrorKind + `docker run` stderr surfacing), spec-fidelity (`pull:` policy / `docker image inspect`), TUI polish (bounded smoke, palette entry, runtime picker, raw-flag editing), and test/doc coverage.

**All 12 high-severity findings are positive confirmations.** There are **no high-severity bugs or gaps.** Every "do not regress" safety invariant is preserved on the docker path. The open punchlist is entirely **medium and below**, and none of it breaks the lab's working deployments.

---

## 1. Safety invariants — all four preserved on the docker path (independently corroborated)

| Invariant | Verdict | Evidence |
|---|---|---|
| **Verify-before-destructive-signal** | ✅ HOLDS | `sidecar.py:164-185` — docker branch calls `verify_container_identity(sidecar)` (id+name+digest, lines 305-318) **before** any `docker kill`/`docker stop -t <grace>`; mismatch → `TrackedProcessMismatch` → surfaced as `identity-verification-failed` (-32002) at `local.py:1480-1503`. A recycled id provably aborts with no destructive command sent. |
| **Scrub-before-wire** | ✅ HOLDS | Secrets emitted as `-e KEY` name-only in argv (`docker_runtime.py:45-46`); values live only in `DockerRunCommand.env`. Container logs stream through the **same** proven scrubbing `LogSink` (`supervisor.py:199-248`, 4096-byte chunks → `sink.feed`). |
| **Crown-jewel (TUI/CLI clean)** | ✅ HOLDS | grep confirms no `subprocess`/`docker`/`pynvml`/`huggingface_hub` execution in `tui/app.py` or `cli.py`. All docker execution is agent-side. The docker work smuggled nothing in. |
| **Live-run remove guard** | ✅ HOLDS (code) | Runtime-agnostic by construction; refuses `config-in-use`/`resource-in-use` on a live sidecar. *Note: the test is hollow (stubs `verify_sidecar_from_system`) — see SA4-A.* |

---

## 2. Spec-compliance scorecard (phase by phase)

| Phase | Status | Notes |
|---|---|---|
| **S1** schema discriminator | ✅ Done | `RuntimeKind` enum + `DockerConfig` + model_validator enforcing docker XOR executable XOR build (`schema.py:21-86`). All 5 validator conditions raise as specified. |
| **S2** error codes | ◑ Mostly | All five (-32018..-32022) defined; `-32021`/`-32022` raised & correct. `-32018/19/20` (image-not-found/name-conflict/daemon-unreachable) **defined but never raised** — dormant pending DockerErrorKind (DK1). |
| **S3** fake-docker harness | ◑ Partial | Functional via **inline** per-test `_write_fake_docker` helpers (real executable stub binaries — good); but **no shared `tests/fakes/fake_docker.py`** and no `tests/fixtures/docker_logs/` the plan specified. |
| **DK0** cmd-gen + masked preview | ✅ Done | serve-strip, secret-safe `-e`, shm-by-TP (32g/16g), DockerConfig defaults, footgun-clean. Argv reconstructed element-by-element vs examples-v1 anchors. |
| **DK1** backend + lifecycle + identity | ✅ Done | `run -d` → capture id → `logs -f` → `wait`; verify-before-stop/kill; sidecar records name+id+digest+grace; eviction; stop-grace. **DockerErrorKind classification missing.** |
| **DK2** logs/health/phase/discover/reattach | ◑ Partial | Log-scrub + discover (`verify_container_running`) + reattach (offset resume) all work. **No docker phase-FSM STARTING→READY test, no docker-log-scrub integration test, DockerErrorKind absent.** Reattach tails durable log rather than re-forking `docker logs -f` (deviation-justified — same design as process runtime). |
| **DK3** eviction + port guard + export + docs | ✅ Done | Sibling eviction (stop+rm), port preflight (incl. `docker ps`), standalone export (`render_standalone_docker_script`), `docs/docker-runtime.md`. `pull:` enforcement + from-wrapper migration helper not done. |
| **DK4** real-hardware validation | ✅ Done | **Authentic** native-docker artifacts: FP8 READY @ `10.25.0.51:18003` (run_id 26d7d486…), BF16 READY @ `:18002` (run_id 2fc7f08b…) — distinct UUIDs/ports/timestamps, returncode 0, configs on disk use `runtime: docker`. |
| **DC0** composer core + presets + validate | ✅ Done | `compose_config` pipeline + `derived[]` provenance; 5 presets (correct names/applies_to); `validate_config_payload` + `_lint_config` (blocks literal secrets). Minor preset-seed deviations from the spec table. |
| **DC1** port/name/run-dir + suggestions | ✅ Done | `allocate_port` scans configured + live sidecars + `ss -ltn` (**3 of 4** spec sources — `docker ps` published ports caught at preflight instead). `engine_suggestions` now populated from model registry + HF `config.json`. TP hint shallow (hard-coded 1). |
| **DC2** save/clone/delete + push/pull/lint | ✅ Done | Atomic 0644 writes, clobber guard, live-run delete guard, push/pull/lint/list all real. `_lint_config` missing exposure-mismatch check (FR-C5); `clone_config` bypasses live-port sources. |
| **DC3** `vela deploy` CLI | ◑ Partial | Full surface: create/edit/clone/list/delete/export + config push/pull/lint, wired to correct RPCs, test-covered. **`--dry-run` untested; idempotency (E5.3) not implemented** (refuses-to-clobber instead — the safer choice, but the spec demands idempotency in 4 places). |
| **DC4** TUI New Deployment wizard | ◑ Partial | Core spine **done**: `n` → compose → validate → preview → review → save → smoke, fully wired, no dead ends; **FlagManager genuinely reused** (not a bespoke wizard). Gaps below (DC4-B/F/G + raw-flag editing). |
| **DC5** docs + polish | ◑ Partial | `docs/docker-runtime.md` + README + `agent-rpc.md` + `test_docs.py` gating exist. `docs/deployments.md` (named in plan) missing; polish gaps. |

**8 of 14 phases fully done, 6 partial, 0 not-yet.** Every partial is "core works; tail of polish/operability/test/doc remains."

---

## 3. The confirmed punchlist (medium; deduped & prioritized)

> P-order = recommended fix order (value × effort). All are **medium or below** — none block the working lab deployments.

**M1 — `docker run` failures are blind (stderr discarded + no DockerErrorKind).** [DK2-D, TA4-A, missed-MEDIUM]
`supervisor.py:183-192` runs `docker run` with `capture_output=True, check=False`; on non-zero it records a bare returncode and **throws away `run.stderr`**. The spec's `DockerErrorKind {IMAGE_NOT_FOUND, IMAGE_PULL_FAILED, DAEMON_UNREACHABLE, NAME_CONFLICT, OCI_RUNTIME_ERROR, GPU_NOT_AVAILABLE}` (§6.5) is entirely absent (grep: zero hits). Operator sees generic CRASHED with no docker text. *Operability gap, not safety.* **Highest-value fix: at minimum scrub+write the stderr; then add the enum + the dormant -32018/19/20 raise sites.**

**M2 — `pull:` policy is dead config + digest is string-parsed, not resolved.** [DK0-H, DK0-I, DK2-B, DK2-C, SA-digest (missed-MEDIUM)]
`docker.pull` is parsed (`schema.py:51`) but **read nowhere** in `src/vela` (independently confirmed: grep for `.pull`/`docker pull`/`image inspect` returns nothing). No `docker image inspect` at launch. `_image_digest_for_sidecar` (`docker_runtime.py:91-94`) string-parses `@sha256:` and **returns the tag verbatim for unpinned images** — so for a tag-only image the sidecar records the tag as "digest" and the anti-reuse digest arm degrades to tag-string equality. Real-world impact contained (lab is digest-pinned + `pull: never`), but `deviation-unjustified` (the schema advertises behavior it doesn't deliver) and it weakens NFR-D2.

**M3 — TUI "Save & Smoke" is open-ended, not bounded.** [DC4-G + missed-LOW] — *small fix*
`app.py:2325-2326` calls `_run_selected_config()` (tails indefinitely). Spec §8 step 6 / E4.3 (P0) want load→READY→auto-stop. A reusable bounded helper **already exists** (`cli.py:2213-2269 _smoke_tui_config_cli`, waits Phase.READY then `action_stop()`) but is only wired to the CLI. **Wire it into the TUI action — low effort.**

**M4 — E4.2 preflight gaps (P0 MVP story).** [E4-2-A]
`preflight.py:23-35` checks port + model-path + world-size (+ docker published-port), but **omits docker image-availability and disk-space checks**, and gated-token is surfaced at compose/launch rather than as a preflight result. With `pull: never`, a missing image fails at launch, not preflight — the "obvious failures caught early" promise is partially unmet. (Pairs with M2.)

**M5 — FlagManager can't add/remove raw passthrough flags.** [missed-MEDIUM, E2.1 P0]
`flag_manager.py:137-145 action_save` copies `config.extra_args` verbatim; passthrough/unknown rendered read-only (246-255); no add/remove binding. Spec §7 / E2.1 require "add/remove passthrough flags (raw)" in customize. (Arbitrary overrides are still reachable headlessly via `vela deploy … --extra-arg`.)

**M6 — Command palette missing "New Deployment".** [DC4-B] — *small fix*
`n` binding works (`app.py:599`), but `get_system_commands` (827-931) has no entry; spec §8 + E1.1 require the palette command as a P0 affordance.

**M7 — Runtime picker limited to Process/Docker.** [DC4-F]
`new_deployment.py:148-153` offers only process/docker; the composer backend supports `executable`/`build` (`composer.py:507-529`). Operator can't create a build-pinned/executable deployment from the wizard (in-scope for DC4 per spec).

**M8 — `vela deploy create` not idempotent + `--dry-run` untested.** [DC3-A, DC3-C]
Refuses-to-clobber without `--overwrite` (the safer FR-C8 path) vs the spec's idempotency clause (FR-C12/NFR-C6/§9/§12). `--dry-run` code exists (`cli.py:1119-1121`) but **no test** exercises it (create or edit).

**M9 — `--ipc=host` AND `--shm-size` both emitted.** [DK2-A] — *benign*
`docker_runtime.py:35-39` always emits both; spec §6.1 frames them as alternatives. Docker ignores `--shm-size` when `--ipc=host` is set, and the lab's own wrappers passed both, so the generated command mirrors the proven wrapper. Cosmetic/contradictory-flag issue, not a break.

**M10 — Test-coverage gaps that leave safety arms unguarded.** [SA1-B/TA3-A, missed-LOW name-arm, SA2-D, TA3-B, SA4-A]
The code is correct; the **tests** are thin where it matters:
- **Image-digest-mismatch** and **name-mismatch** stop-refusal are **untested** — only the `id` arm has a refusal test (`test_sidecar.py:319` short-circuits at id). A regression dropping the digest/name check would pass all tests.
- **No docker-log-scrub integration test** (secret through `docker logs -f` → masked) — the named §10 `test_docker_logs_scrub` doesn't exist; both fake-docker handlers emit no secret.
- **No docker phase-FSM** STARTING→READY walk over fake container logs (DK2 "Done when").
- **Live-run-remove-guard test is hollow** — `test_deployment_composer.py:547-591` monkeypatches `verify_sidecar_from_system` to `True`, exercising neither runtime's real verification.

---

## 4. Verification value — refutations & adjustments (the Opus pass earning its keep)

- **1 refuted:** *DC2-E* — finder claimed `config-in-use` serializes as `-32000`/internal-error over the wire. **Refuted:** it round-trips correctly via `data['target_error_code']` (`rpc_errors.py:54-56`, decoded at `_target_error_code:78-89`). The only real nit: it's not in `ERROR_CODE_BY_NAME` (shares -32000 numeric), which the codebase intentionally supports. Cosmetic, not a bug.
- **9 adjusted (mostly severity down):** *DK2-C* HIGH→LOW (digest degradation only bites unpinned images; lab is pinned); *DC1-A* MEDIUM→LOW (`docker ps` gap at compose is caught at preflight — "no silent collisions" still holds); *DC1-B* reframed (TP mismatch *is* caught at `preflight.py:49`, only the compose-time hint is shallow); *SA4-A* reframed (test is *more* hollow than the finder said). One up: *DC3-C* LOW→MEDIUM (idempotency stated in 4 spec places).
- **Pattern (consistent with prior rounds):** finders were ~92% accurate (111/121 confirmed as-stated); the value of the independent Opus read was catching ~10 over-statements **and** 24 genuinely-missed items (3 medium) — e.g. the discarded `docker run` stderr, the untested name-arm, HF_TOKEN non-injection, the missing `:latest` warning.

---

## 5. Minor/low items worth a sweep (not blocking)

- **HF_TOKEN not auto-injected** for gated docker models (`docker_runtime.py:29` merges only `docker.env`+builder env); spec §6.1 shows `-e HF_TOKEN=<from agent env>`. Lab uses pre-downloaded caches so no impact. [LOW]
- **No `:latest`/unpinned-image warning** (spec §8); **no `--runtime nvidia` option** (relies on nvidia being the default Docker runtime — latent silent-CPU footgun off-lab). [LOW]
- **`gpus: ''` silently drops `--gpus`** with no warning (`docker_runtime.py:31`). [LOW risk]
- **Preset seeds incomplete** vs §6.3 table (balanced missing `--enable-chunked-prefill`; throughput missing batched-tokens/cudagraph; long-context missing max_model_len; low-memory missing enforce_eager). [LOW]
- **`_lint_config` missing exposure-mismatch check** (FR-C5); **compose never emits the canonical non-local-bind warning** for 0.0.0.0 (§6.5 "always emits"). [LOW]
- **`clone_config`/`suggest_deployment_defaults` skip live-port + `docker ps -a` container-name collision checks.** [LOW]
- **`docs/deployments.md` (named in plan) missing**; **no shared fake-docker harness** (S3). [LOW]
- **Old `scripts/blackbird_qwen36_bf16_vllm_foreground.sh` retained** (configs migrated to native docker; wrapper now reference-only, no retirement annotation). [INFO — spec-sanctioned "kept for reference"]
- **`--shm-size 32g` (space) vs examples' `=32g`**; **hf_cache volume appended last + with `:rw`** vs examples' first/no-suffix — all textually divergent from the examples doc but functionally identical (DK0 acceptance is "presence not order"). [INFO]

---

## 6. "How close to done?" — the bottom line

- **Functional MVP (compose → save → native-docker launch → READY → safe stop):** ~95% — **done and hardware-validated** on Blackwell for both FP8 and BF16.
- **Full spec fidelity + all P0 user-story acceptance + test/doc completeness:** ~85%.
- **The single highest-value next step:** **M1** (surface `docker run` stderr + DockerErrorKind) — today an operator is blind on the most common docker launch failures.
- **Then:** M2 (pull/image-inspect → also closes M4's image-availability preflight), M3 (bounded TUI smoke — small), M5/M6 (raw-flag editing + palette entry — P0 affordances), M10 (the safety-arm refusal tests, to lock the invariants against silent regression).

**Net:** the new architecture is **functionally complete and safe for the lab's real workloads**, with a well-scoped, all-medium-or-below punchlist standing between it and a polished, fully spec-compliant v1. The coder stuck closely to the specs; the deviations are mostly justified (safety-first choices) or contained (lab uses digest-pinned + pull:never), and the one architectural shortcut that matters off-lab (string-parsed digest / no `pull`) is clearly identified.

*Snapshot as of 2026-06-06 15:47 EDT, HEAD `4d2fdca`. Read-only review — no code modified.*
