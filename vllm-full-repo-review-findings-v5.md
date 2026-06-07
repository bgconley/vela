# Vela — "Finished" Claim + Rename Validation **v5**

**What this is:** validating the coder's claim that the implementation is **FINISHED**, and that the whole-app **rename to "vela"** was done properly.
**Baseline → HEAD:** `0034df1` → `8ebb3db` — **3 commits**: `a760a99 Add agent token generator and strength checks` (closes V3-6), `cb5dbe3 Rename application to Vela`, `8ebb3db Fix fake child script checkout import`.
**Method (dynamic):** scouted the rename scope + done-surface first, then fanned out **4 Sonnet 4.6** agents scoped to what I found (rename validation, punchlist closure, tests+token-gen security, new-bug sweep); **Opus 4.8** independently verified every load-bearing claim and read the branding/auth code directly.
**Test state:** **746 passed, 0 failed, 0 skipped — deterministically green** (3 agent runs + my own independent run, all 746). **Ruff clean.** All core safety invariants **HELD** through the rename.

---

## 0. Headline

**The "finished" claim is substantially true, and the rename was done properly.** The entire remaining v3 security/robustness punchlist (V3-1…V3-7, N4) is closed with substantive tests; the §17 token generator landed (`secrets.token_urlsafe`, `hmac.compare_digest`); the doc D1 fix shipped; and the rename is complete, correct (upstream vLLM refs preserved), consistent (paths/env/socket/systemd/CI), and **guarded by a real `test_branding.py`**. It is **not** 100% airtight: the token work introduced **one Medium robustness bug** (a misconfigured token silently drops connections), and **two Low doc errata remain open** (D2/D3). None block the single-user v1.

**Completeness:** Agent/controller architecture now **~97–98%** of its v1 scope; overall **~95–96%** to a polished v1. What's left is the one Med fix + Low polish; the only larger gaps (HTTP/WS transports, multi-host overview) are **explicitly out of v1 scope** per the spec.

---

## 1. Rename to "vela" — DONE PROPERLY (high confidence)

| Check | Verdict | Evidence |
|---|---|---|
| Structural | ✅ | `src/vela/` (old `src/vllm_loader/` gone); `pyproject.toml` `name="vela"`, `vela = "vela.cli:main"`, `packages=["src/vela"]`; `import vela.*` + entrypoint resolve. |
| No residual app-name in **live** surfaces | ✅ | Sweep of src/tests/docs/scripts/packaging/pyproject/README/.github found the old name **only** in `test_branding.py`'s forbidden-list (intentional) — plus historical `artifacts/**`, old punchlists, and my own findings files (correctly retain it). `scripts/fake_vllm_child.py` now `from vela.fake_child`. |
| Upstream vLLM refs **preserved** (not over-renamed) | ✅ **[Opus-verified]** | `import vllm`, `vllm serve`, `VllmProfile`×28, `wheels.vllm.ai`, `vllm==`, `vllm.entrypoints`, default exec `"vllm"` all intact in `src/vela/`. No `vela serve`/`vela.entrypoints` over-rename. |
| Functional consistency (paths/env/socket/systemd/CI) | ✅ | state `~/.local/state/vela`, config `~/.config/vela`, builds `~/.local/share/vela/builds`, socket+`agent.json`, `VELA_*` env (code **and** `run_remote_tests.sh` **and** `.github/workflows/remote-validation.yml`), `packaging/systemd/vela-agent.service` (`ExecStart=vela agent run`), handshake `vela_version`, ControlPath `~/.ssh/vela-%C`, `DEFAULT_AGENT_COMMAND=("vela","agent","connect")`. No `VELA_X`-vs-`VLLM_LOADER_X` split that would break validation. |
| Runtime identity matching | ✅ | Sidecar identity uses hash/PID/create_time/PGID — no hardcoded old-name string in the matcher; rename can't break process verification. |
| Branding guard test | ✅ **[Opus-read]** | `tests/test_branding.py` (4 tests): pyproject branding; runtime path/constant branding; `python -m vela.cli --help` is "Vela"-branded and contains **none** of 7 old names; **live docs+scripts contain none of the 7 old names**. All pass. |

**Verdict: complete, correct, consistent — and self-guarding.** This is a clean rename.

---

## 2. "Is it done?" — remaining punchlist closure

| Item | Status | Evidence |
|---|---|---|
| **V3-1** read-loop catches oversized-line/`RecursionError` | ✅ FIXED | `stdio.py:52-63`/`ndjson.py:28-29`; tests `…closes_cleanly_on_reader_limit_error`, `…reports_deep_json_parse_error_and_continues` |
| **V3-2** block `-A`/`ForwardAgent`/`*Forward` | ✅ FIXED | `factory.py:56,80-87,157-256`; parametrized rejection tests |
| **V3-3** reject `-o=` empty-key | ✅ FIXED | `factory.py:211-217`; `…rejects_malformed_ssh_option_assignments` |
| **V3-5** remove `auth_state=None` default | ✅ FIXED | `stdio.py:93-99` (no default); `…requires_explicit_auth_state` |
| **V3-7** local deep-verify asserts hash **values** | ✅ FIXED | `test_agent_client.py:5490` asserts exact `_sha256_uri(...)` per file |
| **N4** real >64 KB frame round-trip | ✅ FIXED | `test_rpc_framing.py:67` (1 MiB through real reader) + 128 KiB over subprocess |
| **V3-6** `vela agent gen-token` + strength | ✅ FIXED | `auth.py` `secrets.token_urlsafe`, 256-bit default; CLI floor; tests |
| **D1** "open the TUI" command | ✅ FIXED | `@app.command("tui")` now exists (`cli.py:118`); README documents bare `vela` **and** `vela tui` |
| **D2** doc `inprocess` → `in_process` | ❌ **OPEN** **[Opus-verified]** | `docs/configuration.md:29` still `inprocess`; the enum value is `in_process` → copying the example fails Pydantic validation (test/dev transport only) |
| **D3** build-vs-model `--force` doc | ⚠️ **OPEN** | `builds-and-models.md:52-53` still reads as if `build remove` has `--force`; it has only `--yes` (models have `--force`) |

**The functional/code punchlist is fully closed.** The only open items are two **Low** doc errata.

---

## 3. New findings (introduced by the token-gen work)

### N5-1 — Misconfigured `VELA_AGENT_TOKEN` silently drops every connection **[Opus-verified, Med]**
`serve_agent_stream` builds `auth_state = _ConnectionAuthState()` at `stdio.py:44`, **outside** the `try:` (begins `:49`). `_ConnectionAuthState.__init__` → `configured_agent_token()` → `validate_agent_token()` **raises `AgentTokenError`** if `VELA_AGENT_TOKEN` is set but weak/short/has-whitespace. That propagates into `handle_connection`'s `try/finally` (`socket.py:42-47`, **no `except`**) → the asyncio connection task dies uncaught → **the daemon stays up but every connection is dropped**, and the *helpful* "generate one with `vela agent gen-token`" message is swallowed (logged as an asyncio traceback, never surfaced to the operator). Controller-side, `handshake_params` (`client.py:40`) raises the same way on a malformed controller token. Fail-closed and misconfig-only, but a confusing failure mode for a brand-new security feature. **Fix:** validate the env token once at daemon/connect startup (fail fast with the clear message), or catch `AgentTokenError` in `handle_connection` and return a clean error frame.

### N5-2 — Token strength check is length-only **[Opus-verified, Low]**
`validate_agent_token` (`auth.py:32`) rejects `<22` chars or whitespace — nothing else; a 22-char low-entropy string (`"aaaa…"`) passes, though the message claims "128 bits of entropy." The *generator* is sound (CSPRNG); only a hand-typed env token could be weak-but-long. Acceptable trade-off for a shared secret, but enforcement doesn't match the wording.

### N5-3 — Redundant min-check in `agent gen-token` **[Low]**
`cli.py` pre-checks the byte floor and exits 2 before `generate_agent_token` re-checks it — the `auth.py:24-28` raise is unreachable from the CLI. Dead guard, not a bug.

---

## 4. Invariants & security (all verified)
Crown-jewel **clean** in `vela/tui/app.py`+`vela/cli.py`; scrub-before-wire intact; live-run remove guard + force-can't-bypass intact; verify-before-signal intact; socket/path consistency post-rename. Token gen: CSPRNG, 256-bit default, `hmac.compare_digest`, never logged, frictionless-local preserved (`configured_agent_token()`→`None` when unset). The `8ebb3db` fake-child fix is correct (no deeper rename gap).

---

## 5. Definition of done — what's actually left
**For the §13 single-user v1, this is effectively done.** To make "finished" airtight:
1. **N5-1 (Med)** — make a malformed `VELA_AGENT_TOKEN` fail *loudly and early* instead of silently dropping connections. *(only non-cosmetic item)*
2. **D2 (Low)** — `inprocess` → `in_process` in `configuration.md`.
3. **D3 (Low)** — clarify the build-vs-model `--force` asymmetry.
4. *(optional)* N5-2 entropy-vs-length wording; the deferred §17/§future items (HTTP/WS transports, multi-host overview) remain out of v1 scope.

**Bottom line:** the coder's "finished" is fair — the architecture is complete, the rename is clean and guarded, 746 tests are deterministically green, and the security punchlist is closed. I would not call it *signed-off* until N5-1 is fixed (a misconfigured token shouldn't silently brick the agent), but that's a small, well-localized fix, not remaining feature work.

> No code was modified in this review. This report is the only output written to the repo.
