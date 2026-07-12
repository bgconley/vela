# Vela — Seamless Remote Onboarding & Self-Healing Targets — Design Spec (v1)

**Purpose:** turn the six setup "gotchas" into one-command, self-diagnosing flows. **Audience:** the engineer(s) extending `vela`. **Status:** spec-ready recommendations — no code written here.
**Grounding:** every recommendation reuses existing seams (the SSH transport `transport/factory.py:_remote_agent_command`, the targets registry `config/targets.py`, the agent handshake `agent/local.py:_handshake` `host_info`, the `check_build_prerequisites` RPC that already returns `uv_available`, the atomic config-write path behind `update_config_flags`, and the named failures `AGENT_NOT_INSTALLED`/`AGENT_UNREACHABLE`/`AGENT_VERSION_MISMATCH` from agent-spec §7.8). Two recommendations *realize spec-deferred items*: `push_config` (agent-spec §10.3) and controller-driven agent bootstrap (§17).

---

## 0. Design principles

1. **One command to onboard a target** — discover/install/verify in a single guided flow; never hand-wire paths.
2. **Every failure names its fix** — no silent drops; an error carries a one-line remediation *and* the exact command to run.
3. **Managed, not manual** — vela should own the things it asks operators to set by hand (the agent install location, the token, the config sync).
4. **Idempotent + introspectable** — re-running setup is safe; `vela doctor` shows the resolved truth on both hosts.
5. **Don't fight the architecture** — configs stay agent-side (they reference host-local paths); we add *sync*, not relocation.

---

## 1. Headline: two new commands that absorb most gotchas

### `vela targets bootstrap <name> --host <user@host> [--install] [--build <spec>] [--ssh-key <path>]`
One guided flow that collapses gotchas **1, 3, 4, 5**:
1. **SSH reachability + auth** probe; on failure, offer `vela targets setup-ssh` (ssh-copy-id) — R5.
2. **Agent discovery** (R1): find `vela` on the target; if absent and `--install`, install it into the canonical location and record the **absolute** agent path.
3. **Build readiness** (R4): with `--build`, preflight `uv`/toolchain and create a default managed vLLM build.
4. **Write the target config** with the auto-resolved agent path/venv/workdir (no manual `--venv`).
5. **Handshake test** + a green/red summary, each red line carrying its fix.

*Implementation:* a new `targets bootstrap` Typer command orchestrating existing pieces — an SSH probe (reuse the `transport/factory.py` SSH option builder), remote install (reuse `scripts/rsync_to_gpu.sh` or `pip install 'vela @ git+…'`), the `create_build` RPC, and `targets test`. *Acceptance:* `vela targets bootstrap blackbird --host bgconley@10.25.0.51 --install --build 'vllm==0.11.2'` yields a connectable, launch-ready target with zero hand-edited fields.

### `vela doctor [--target <name>] [--json]`
One read-only command that introspects **controller + target** and names every gotcha's fix. Reports: vela version match (controller vs agent), SSH reachability/auth, resolved agent path, Python/`uv`/CUDA/driver/GPU on the target, the resolved per-host dirs (config/runs/builds/models/socket), token/auth status, and active build/model. *Implementation:* extend the handshake `host_info` (`agent/local.py:506`) into a structured `host_report`, add a `doctor`/`diagnose` RPC that runs the cheap probes agent-side, and render a checklist controller-side. *Acceptance:* a misconfigured target shows a red line + exact remediation for each failing check; a healthy one is all green.

---

## 2. Per-gotcha recommendations

### R1 — Kill the `--venv`/PATH footgun (auto-resolve the agent) — gotcha #1 *(highest value)*
**Problem:** the SSH command is `PATH=<venv>/bin:$PATH … vela agent connect` (`factory.py:_remote_agent_command`), so it breaks unless the operator sets `venv:` correctly and non-interactive SSH lacks `~/.local/bin`.
**Spec:**
- Add optional `target.agent_command: list[str] | None` (absolute) to `TargetConfig` (`config/targets.py`). When set, the transport invokes it verbatim — **no PATH dependency**.
- Define a **canonical self-managed install path**: `~/.local/share/vela/venv/bin/vela`. `bootstrap --install` installs there.
- During `targets add`/`bootstrap`/`test`, run an SSH **discovery probe** trying, in order: `command -v vela` → `~/.local/share/vela/venv/bin/vela` → `~/venvs/vela/bin/vela` → `<venv>/bin/vela` (if `--venv` given) → `python3 -m vela`. Pick the first that returns a compatible `vela --version`; store it as `agent_command`.
- If none found → `AGENT_NOT_INSTALLED` naming `vela targets bootstrap <name> --install`.
**Implementation:** extend `_remote_agent_command` to prefer `target.agent_command`; keep `venv`/`workdir` as overrides. The version check reuses the handshake `vela_version`. *Acceptance:* `targets add --host …` with **no** `--venv` connects when vela is installed in any standard location.

### R2 — Config authoring on the controller, synced to the target — gotcha #2 *(realizes spec §10.3 `push_config`)*
**Problem:** discovery is agent-side (correct), but operators want to edit where the TUI runs.
**Spec / CLI:**
- `vela config push <file> --target <name>` → agent writes it into its config dir (atomic, validated) — reuse the `update_config_flags` write path.
- `vela config pull <name> --target <name>` → fetch a target config to stdout/file.
- `vela config edit <name> --target <name>` → pull → `$EDITOR` → push round-trip.
- `vela config lint <file>` → flag **host-local absolute paths** (`model:/abs`, `command.executable:/abs`, `command.cwd:/abs`) and recommend the **portable** form (`model_ref` + `command.build`, agent-spec-encouraged) so the config is target-agnostic.
- TUI: the config picker shows the active target and offers **"Push this config…"** when a local config isn't present on the target.
**Implementation:** new RPC `push_config`/`pull_config`/`list_config_files` (agent-side, validated, atomic) + a `vela config` Typer group. *Acceptance:* author a portable config on P620, `vela config push … --target blackbird`, launch it — no SSH-and-edit.

### R3 — Make build prerequisites proactive, not a mid-job failure — gotcha #3
**Problem:** picking `nightly`/`commit` fails with `feature-unavailable: requires uv` *after* the operator commits to it.
**Spec / CLI:**
- `vela build doctor --target <name>` → reports python, `uv`, CUDA toolkit, driver, GPU arch, and **which build methods are available** with install hints (reuse the existing `check_build_prerequisites` RPC → `uv_available`).
- The create-build UI/CLI **preflights before the job** (the precheck already exists for the TUI form) and, for `nightly`/`commit` without `uv`, offers: **(a) install uv now** (`curl -LsSf https://astral.sh/uv/install.sh | sh` or `pip install uv`, run agent-side as a streamed job), or **(b) fall back to a compatible method**.
- `bootstrap --build` runs this preflight first.
**Implementation:** extend `check_build_prerequisites`; add an optional agent-side "install uv" job (same streamed-job infra as build installs). *Acceptance:* selecting `nightly` on a uv-less host surfaces the requirement + a one-keypress install before any failure.

### R4 — SSH auth: diagnose it, set it up, surface it — gotcha #4
**Problem:** `BatchMode=yes` (correct) means a failed key auth = silent drop.
**Spec / CLI:**
- Capture SSH **stderr** on the bridge; map exit 255 → `AGENT_UNREACHABLE` with the *actual* SSH message ("Permission denied (publickey)") + remediation.
- `vela targets setup-ssh <name>` → guided `ssh-copy-id` of the controller's key to the target.
- First-class key on the target: `vela targets add --ssh-key <path>` (stores it so the transport passes `-i <key>` via the hardened option path) instead of only the `ssh_opts_env` indirection.
- Default **ControlMaster** (agent-spec §7.2) for near-instant reconnects.
**Implementation:** the SSH transport already separates stdout (protocol) from stderr (diagnostics) — route stderr into the named-failure banner; add `setup-ssh`; add `--ssh-key` → an allowed `-i` option (already permitted by the SSH-option allowlist). *Acceptance:* a passwordless-not-configured target yields "SSH auth failed: publickey — run `vela targets setup-ssh blackbird`," not a silent hang.

### R5 — Make per-host paths visible (and overridable) — gotcha #5
**Problem:** config/runs/builds/models dirs differ per host and are invisible.
**Spec:** `vela doctor` and `vela agent status` print the **resolved** paths on each host (config `~/.config/vela`, runs `~/.local/state/vela/runs`, builds `~/.local/share/vela/builds`, models registry, socket, version). Document `VELA_CONFIGS`/`XDG_*` overrides in one place. *Implementation:* expand `host_info`/`host_report` to include the resolved dirs; render in `doctor`/`agent status`. *Acceptance:* one command answers "where does this host keep its stuff."

### R6 — Token auth: managed file + fail-loud (and fix the silent-drop bug) — gotcha #6
**Problem (and a real bug):** operators must set the *same* `VELA_AGENT_TOKEN` on both hosts by hand, and a **malformed value silently drops every connection** (the v5 finding **N5-1**: `_ConnectionAuthState()` is built outside the `try` in `stdio.py:44`; `handle_connection` in `socket.py` has no `except`, so `AgentTokenError` kills the connection task with only an asyncio log).
**Spec:**
- **Fix N5-1 first (must-fix):** validate the token once at **daemon startup** and at **connect** with a clear, surfaced error — never a silent drop. (Catch `AgentTokenError` in `handle_connection`/`serve_agent_stream` and return a clean `agent-auth-required`/`invalid-token` frame; validate eagerly in `agent run`/`agent connect` startup.)
- **Token as a managed file**, not a hand-copied env var: `vela agent gen-token --install` writes `~/.config/vela/agent-token` (0600) on the host and (for SSH targets) pushes the matching token to the target's file; both sides read the file if `VELA_AGENT_TOKEN` is unset. Removes the "set the same value on both boxes" step.
- `vela doctor`/`vela targets test` report auth status: `none | required+provided | required+missing | mismatch | malformed-token`.
**Implementation:** small change in `agent/auth.py` (file fallback for `configured_agent_token`), the startup/connect validation fix, and a `gen-token --install` that writes+pushes the file. *Acceptance:* `vela agent gen-token --install --target blackbird` enables auth on both hosts in one command; a bad token yields a named error, not a silent drop.

---

## 3. Cross-cutting: name every failure with its fix
Wire concrete remediation text + the exact command into the already-existing named banners (`AGENT_NOT_INSTALLED` → `targets bootstrap --install`; `AGENT_UNREACHABLE` → `targets setup-ssh` / the SSH stderr; `AGENT_VERSION_MISMATCH` → "upgrade the agent: `vela targets bootstrap <name> --install`"; build `feature-unavailable: uv-required` → `build doctor` + install-uv). This is the cheapest, highest-leverage change — it turns every stumbling block into a self-service fix.

---

## 4. Priority & phasing
- **P0 (correctness):** fix **N5-1** (silent token drop) — it's a live bug regardless of any UX work.
- **P1 (kills the top stumbling blocks):** **R1** (auto-resolve agent / drop `--venv`) + the **named-failure remediations** (§3). Small, high-leverage.
- **P2 (the headline):** **`vela targets bootstrap`** (§1) + **`vela doctor`** — fold R1/R3/R4/R5 into one onboarding command and one introspection command.
- **P3 (workflow polish):** **R2** `vela config push/pull/edit/lint` + **R6** managed token file.
- **P4 (nice-to-have):** auto-install `uv`, ControlMaster-by-default, TUI "Push config…"/"Bootstrap target…" affordances.

**Bottom line:** the smallest set that removes most pain is **fix N5-1 + R1 + named-failure remediations**; the most impactful single feature is **`vela targets bootstrap`** (it operationalizes the spec's deferred §17 auto-bootstrap), with **`vela doctor`** as its diagnostic twin. None of this changes the architecture — it adds the onboarding/self-heal layer the controller/agent split was already designed to support.

> No code was modified. This document is the only output written to the repo.
