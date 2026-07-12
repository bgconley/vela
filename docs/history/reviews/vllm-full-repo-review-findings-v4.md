# vLLM Loader — Docs & Original-Punchlist Review **v4**

**What this is:** the follow-up the prior three findings didn't cover — the **original v1 agent punchlist** (`vllm-agent-architecture-review-punchlist.md`, never reviewed before; v2–v5 were) and the **`docs/` reference set + README** (the "additional spec docs"), audited for **accuracy against the implemented code** (not against the design specs — these docs describe what's built).
**HEAD:** ~`1f473c7` (live-moving). **Method:** 2 **Sonnet 4.6** doc-accuracy auditors cross-checking every doc claim against `cli.py`/`agent/local.py`/`schema.py`/`targets.py`/`factory.py`/scripts, then **Opus 4.8** independently verified each load-bearing claim and corrected one. `[Opus-verified]`/`[Opus-corrected]` tags as before. **No code modified.**

---

## 0. Headline

**The docs are substantially accurate and, notably, kept in sync with live code** — `configuration.md` already documents the SSH-option rejections from commits that landed *during* the prior review (`RequestTTY`, `ForkAfterAuthentication`, `SessionType`, `StdinNull`). The **original v1 punchlist is fully closed** (its last unconfirmed item, `discover_runs_no_paths` dispatch, is now live). The doc audit surfaced **5 real drift items — 1 Medium, the rest Low** — plus omissions; none are architectural, all are quick text fixes. One agent finding was a misread and is dismissed.

---

## 1. Original v1 punchlist (`vllm-agent-architecture-review-punchlist.md`) — CLOSED

This was the genesis review (PA0–PA4, 329 tests). Its items were marked closed in v2; I've now read it in full and confirmed closure against current code:

- **v1-P1 reachable_url loopback** → CLOSED. Controller-side rewrite from the SSH target host (verified in finding v1).
- **v1-P2 PA5 controller UI** → CLOSED. Header `⊕ target` segment, `TargetManagerScreen`, `t`/`R` keys, named banners, disconnected guard — all present (finding v1 UI audit).
- **v1-P3 protocol contract** → CLOSED. Integer JSON-RPC codes + `data` key (`rpc_errors.py`); handshake downgrade; **`discover_runs_no_paths` now dispatched** `agent/local.py:392` `if method in {"discover_runs","discover_runs_no_paths","discover_detached"}` **[Opus-verified — this was the one item the original punchlist explicitly flagged as advertised-but-not-dispatched; it is now closed]**.
- **v1-P4 robustness** → CLOSED. Graceful log-rotation resume, exponential reconnect backoff, GPU push events (verified across v1/v2).
- **v1 deferred** (systemd unit, `--idle-timeout`, ControlMaster default) → addressed (systemd unit present; `Q11` in v3).

The original PA0 plan doc (`docs/superpowers/plans/2026-06-03-agent-pa0-local-targets.md`) is a clean TDD slice spec, fully realized.

---

## 2. Docs accuracy audit (the "additional spec docs")

### Verified ACCURATE against code
- **RPC method list** (`agent-rpc.md`): all ~42 documented methods are dispatched in `handle()` (or handled in the stdio layer for `subscribe`/`unsubscribe`). No documented-but-missing method. **[partially Opus-verified]**
- **CLI commands** (README/`configuration.md`/`builds-and-models.md`): every documented command/flag exists in `cli.py` **except** `vllm-loader tui` (D1). `targets`/`build`/`model`/`agent` subcommands + flags (`--target`/`--method`/`--spec`/`--deep`/`--json`/`--socket`/…) and the `[dev]` extra all verified.
- **Config fields, precedence, exposure** (`configuration.md`): every documented field exists in `schema.py`; build precedence (`executable>build>default>PATH`) and model precedence (`model_ref+rev>model+rev>model`) match the resolver; exposure `local/lan/public` matches the validator. `launch.mode` is correctly described as a "compatibility label; all launches supervised" (`local.py:850` always supervises). **[Opus-spot-verified]**
- **SSH rejection list** (`configuration.md`): **every option the doc names as rejected/allowed is correct** against `factory.py` (it just isn't exhaustive — see D5).
- **builds-and-models.md**: build-method/uv-fallback table, `install.log` 0600, dedup-aware model GC, `model verify --deep` (local *and* HF), launch composition — all accurate.
- **gpu-workflow.md**: every referenced script/workflow/artifact exists; documented `VLLM_LOADER_REMOTE_*` env vars are actually read by `run_remote_tests.sh`.
- **Security claims** (`agent-rpc.md`/README): controller-passes-only-ids, verify-before-signal, unconditional agent-side scrub, no raw-log RPC, `VLLM_LOADER_AGENT_TOKEN` handshake gate — all match code.

### Drift items (the fixes)
| ID | Sev | Doc | Issue (Opus-verified) | Fix |
|---|---|---|---|---|
| **D1** | **Med** | README:27 | `vllm-loader tui` does **not** exist (`cli.py` uses `invoke_without_command=True`; bare `vllm-loader` launches the TUI). A user following Quickstart literally gets "No such command 'tui'". | Change doc to bare `vllm-loader`, or add a `tui` alias command. |
| **D2** | Low-Med | configuration.md:29 | `local_transport: inprocess` — the enum value is **`in_process`** (`targets.py:20`); `inprocess` fails validation. Only affects the test/dev transport (`socket` is the real path). | Fix the doc value to `in_process`. |
| **D3** | Low-Med | builds-and-models.md:53 | "Config-pin protection can be overridden with `--force`" — true for **models only**. `build_remove` (`cli.py:484`) has only `--yes`, **no `--force`** (`build_registry.remove_build` takes no force). | Clarify the build/model asymmetry (or add `--force` to build remove if intended). |
| **D4** | Low | agent-rpc.md:92 | `error` listed as a discrete event kind, but there is **no `AgentEvent("error",…)`** — errors fold into `phase`/`exited`/`health`/`job_done` via `error_kind`/`error_excerpt` (exactly as agent-spec §9 prescribes). | Drop `error` from the event list or note it's folded. |
| **D5** | Low | configuration.md:30-49 | SSH rejection list is **accurate but incomplete** — omits `PermitLocalCommand`, the full `-D/-L/-R/-N/-W/…` flag set, `GlobalKnownHostsFile`; and understates the allow-list (`-4/-6/-C/-q/-x`, `-b/-c/-E/-e/-m`). Everything it *does* name is correct. | Optional: note "representative, not exhaustive," or regenerate from the code sets. |

**Omissions (not wrong, just undocumented):** 5 real schema fields absent from the docs — `vllm.require_flags` (the hard pre-launch gate), `launch.health` (path/interval), `logging.max_log_len`, `model.description`, `target.socket_path`; 3 extra typed sidecar fields (`build_label`, `model_repo_id`, `model_commit_sha`); the `VLLM_LOADER_REMOTE_ARTIFACT_NAME` env var. Worth adding `require_flags`/`launch.health` since they're operator-meaningful.

### Scrutiny correction
- **`typed_sidecar_resources` is NOT an undispatchable-method bug [Opus-corrected].** It's a **capability feature-flag** (in `client.py:28` required-caps and `local.py:122` advertised-caps), negotiated at handshake like `builds`/`models`/`gpu` — never a callable method. An auditor compared the capability list to the method-dispatch table and flagged it; that's a category error. Working as designed.

---

## 3. Fix list (all doc-only; no code change required for v1)
1. **D1** — README: `vllm-loader tui` → bare `vllm-loader` (or add the alias). *(the one Medium; first thing a new user hits)*
2. **D2** — `in_process` value typo.
3. **D3** — clarify build-vs-model `--force` asymmetry.
4. **D4/D5** — `error`-event wording; mark the SSH list representative.
5. *(completeness)* document `vllm.require_flags` and `launch.health`.

**Bottom line:** the "other punch" (v1) is closed, and the additional spec docs are accurate and unusually well-maintained — the remaining work is a handful of small text corrections, led by the `vllm-loader tui` Quickstart fix. This does not move the v3 completeness numbers (the architecture is unchanged); it tightens the docs around it.

> No code was modified in this review. This report is the only output written to the repo. HEAD is live-moving (`1f473c7`); a couple of doc items may already be patched.
