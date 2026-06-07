# Vela — Session Context & Handoff (2026‑06‑04 → 2026‑06‑06) — COMPREHENSIVE

**Purpose:** a self‑contained, detailed record of an extended working session so a future session (human or agent) can resume cold without re‑deriving anything. It preserves: the project design, the lab infrastructure, five rounds of multi‑agent review with the **full findings** (not just summaries), the rename validation, the real deployment created, the forward feature specs, and — critically — the fact that **the agentic coder is actively implementing those forward specs right now**.

**Repo:** `/Users/brennanconley/vibecode/lab-tui` (Mac author box) · git remote `https://github.com/bgconley/vela.git` · branch `main`.
**App:** **`vela`** (renamed this session from `vllm-loader` / package `vllm_loader` / repo dir `lab-tui`). Version `0.1.0`. Python ≥3.10, hatchling, console script `vela = vela.cli:main`. OpenWolf‑managed (`.wolf/`; protocol `.wolf/OPENWOLF.md`).
**HEAD (committed) at session end:** `2542867` "Harden agent token misconfiguration handling." **Tests:** 749 collected, deterministically green (grew 580→746→749). Ruff clean.

> **GIT DISCIPLINE — read this:** I (the reviewer) **committed nothing and pushed nothing** all session. A **separate agentic coder commits live** to this same repo. As of session end the working tree **also contains the coder's uncommitted in‑progress implementation** of the deployment‑composer + docker‑runtime specs (see Part XI). Do **not** assume a clean tree; re‑establish `git status`/`git log`/test state before acting.

---

## 0. TL;DR (expanded)
1. **The app is essentially v1‑done for its single‑user scope.** Agent/controller architecture ~95–96%; overall ~92–94% to a polished v1. 749 tests deterministically green, ruff clean, all core safety invariants intact, validated on real Blackwell hardware via self‑hosted GitHub Actions.
2. **48 commits across the session's review window** (`c20d6c1..2542867`). The coder closed the entire review punchlist (P1–P9, Q1–Q12, N1–N6, V3‑1..V3‑7, D1) with regression tests, plus a substantial SSH/auth security‑hardening wave, plus the `vela` rename.
3. **The rename was done properly** (complete, correct — upstream vLLM preserved, consistent, branding‑guard test).
4. **The last product bug I flagged (N5‑1, misconfigured token silently dropping connections) was addressed** at HEAD `2542867`.
5. **Operational reality (via SSH):** Qwen3.6 27B runs on **Blackbird** from the **Docker image** `vllm/vllm-openai@sha256:b13d6e5…` (vLLM `0.20.2rc1.dev9+g01d4d1ad3`). I created + validated a native Vela config for the **BF16** variant.
6. **Forward features specced this session** (onboarding, composer, docker runtime) — **and the coder has already begun implementing the composer + docker runtime** (uncommitted; Part XI).

---

# PART I — THE PROJECT (`vela`)

## I.1 What it is
A phase‑aware **Textual TUI + Typer CLI** for launching, monitoring, and managing **vLLM** servers from named YAML configs, with managed vLLM **builds**, a model **registry**, and a **controller/agent** split (TUI on a workstation, GPU box does the work). It spawns/monitors `vllm serve` (or a Docker container) as a child; it never `import`s vllm.

## I.2 The four design specs (repo root)
- `vllm-tui-loader-spec-v2-CANONICAL.md` — config schema; command builder (the **version‑aware flag‑emission rule:** emit a flag only when desired ≠ installed default; never bake vLLM defaults); attached PTY + detached supervisor; scrubbing LogSink (incremental UTF‑8 decode‑then‑split on \r/\n, 1 MiB bounded partial‑line, 0600 durable); phase FSM + error kinds (OOM/PORT_IN_USE/MODEL_NOT_FOUND/TP_MISMATCH/HF_AUTH/CRASHED/TIMED_OUT); health (`/health` unauth → READY, `/v1/models` Bearer); GPU (NVML+nvidia‑smi); sidecar/manifest + 5‑check identity; TUI/UX (§8); security (api‑key scope caveats, exposure model).
- `vllm-build-management-spec-v1.md` — per‑build venvs under XDG data; methods pip/nightly/commit/git/wheel/adopt; `uv` preferred (nightly/commit **require** uv; pip can't honor index priority); two‑tier flock + refcount‑by‑verified‑sidecar; integrity (freeze/exec sha + `vllm --version`+`import vllm` agree); FlagManager (modeled/passthrough/unknown); independent‑run artifacts (`run.sh`/`bin/`); precedence `executable > build > default > PATH`.
- `vllm-model-management-spec-v1.md` — registry as a **catalog over the shared HF cache** (not an owned store): `scan_cache_dir` merge, pin `repo@commit_sha` via `HfApi().model_info`, streamed `snapshot_download`, **dedup‑aware GC** via `delete_revisions(...).expected_freed_size`, refuse‑if‑live/pinned; schema `revision`/`model_ref`; precedence explicit `model`+`revision` > `model_ref` > bare `model`.
- `vllm-agent-architecture-spec-v1.md` — controller/agent; per‑host daemon; NDJSON‑RPC; idempotent launch (controller‑minted run_id); verify‑before‑signal agent‑side; scrub‑before‑wire; warm `seq` buffer + offset resume; targets registry; **crown jewel** (controller no authority); §17 future = HTTP/WS transports, multi‑host overview, capability tokens.

Earlier punchlist series `vllm-agent-architecture-review-punchlist*.md` (v1–v5) = the coder's *pre‑my‑review* history; all CLOSED (the v1 one's last open item, `discover_runs_no_paths` dispatch, is now live).

## I.3 Architecture invariants (the "do not regress" set — verified every round, with anchors)
- **Crown jewel:** `grep -nE "current_process|Popen|os.kill|killpg|getpgid|scan_cache_dir|snapshot_download|delete_revisions|pynvml|openpty" src/vela/tui/app.py src/vela/cli.py` → **clean.**
- **Verify‑before‑every‑destructive‑signal** (agent‑side): `agent/local.py` `_stop`/`_kill` → `_request_stop_signal`/`_request_kill_signal` → `engine/sidecar.py` `stop_sidecar_from_system`/`destructive_signal` → `verify_sidecar_identity` (5 checks: pid+create_time, procfs_starttime, pgid, command_hash, supervisor identity) before `os.killpg`. Mismatch → `TrackedProcessMismatch` → `identity-verification-failed` (`-32002`).
- **Live‑run remove guard + force can't bypass:** `engine/build_registry.py`/`engine/model_registry.py` enumerate verified live sidecars and refuse `resource-in-use`; `force` overrides only the config‑pin, never a live run. Pinned by `test_agent_refuses_to_force_remove_model_used_by_live_run`.
- **Scrub‑before‑wire:** `engine/log_sink.py` `_commit` → `engine/redaction.py` `scrub_text` (masks `api_key`/`HF_TOKEN` literals + `Authorization: Bearer \S+` + `\b(?:sk-|hf_)[^\s"'&;,\]})]+`) before the durable write *and* the event emit. No raw‑log RPC.
- **Daemon:** Unix socket (`0700` dir / `0600` socket), `agent.json` identity, **SO_PEERCRED** (`agent/socket.py verify_same_user_peer`), no network port; auto‑spawn on connect; systemd `packaging/systemd/vela-agent.service`.
- **Transports:** `transport/factory.py` (SSH command build + the SSH‑option allow/deny hardening), `transport/socket.py` (UDS), `transport/subprocess.py` (SSH bridge), `transport/inprocess.py` (in‑process test agent), `transport/ndjson.py` (`MAX_FRAME_BYTES = 2 MiB`, `FRAME_STREAM_LIMIT = +1`), `transport/rpc_errors.py` (the code map, §App).

## I.4 Config schema (`src/vela/config/schema.py`) — full field reference
`ModelConfig`: `name`, `target?`, `description?`, `model`, `revision?`, `model_ref?`, `served_model_name?`, `command`, `engine`, `server`, `logging`, `env`, `extra_args`, `launch`, `vllm`.
- `CommandConfig`: `runtime: process|docker` *(new, coder in progress)*, `entrypoint: serve|module`, `executable?`, `build?`, `cwd?`, `docker?` *(new)*.
- `DockerConfig` *(new, coder in progress)*: `image`, `container_name?`, `gpus="all"`, `ipc_host=True`, `shm_size?`, `network="host"`, `volumes[]`, `hf_cache?`, `hf_cache_target="/root/.cache/huggingface"`, `env{}`, `restart="no"`, `stop_grace_seconds=90`, `entrypoint?`, `pull="never|missing|always"`, `evict[]`, `extra_run_args[]`.
- `EngineConfig` (all **unset/None** so installed vLLM defaults win): `tensor_parallel_size`, `pipeline_parallel_size`, `gpu_memory_utilization`, `max_model_len`, `dtype`, `quantization`, `kv_cache_dtype`, `load_format`, `enforce_eager`, `swap_space`, `block_size`, `seed`, `max_num_seqs`.
- `ServerConfig`: `host="127.0.0.1"`, `port=8000`, `exposure=local|lan|public`, `api_key?`, `probe_host?`.
- `LoggingConfig`: `request_logging=False`, `suppress_access_log_for[]`, `max_log_len?`.
- `VllmConfig`: `version_profile?`, `require_flags[]` (hard pre‑launch gate).
- `LaunchConfig`: `mode=attached|detached` (compatibility label — all agent launches are supervised), `ready_timeout_seconds=900`, `health{path,interval_seconds}`, `runs_dir?`.
`extra="forbid"` throughout. `command.executable`/`command.build`/`command.docker` mutually exclusive.

## I.5 Agent RPC surface (43 methods, all dispatched; `docs/agent-rpc.md`)
`handshake, ping, list_configs, update_config_flags, preview, preflight, prepare_launch, launch, wait, stop, kill, restart, status, gpu, sample_gpus, health, probe_until_ready, tail_detached, discover_runs, discover_runs_no_paths, discover_detached, reattach, reattach_detached, list_builds, adopt_build, inspect_build, select_build, verify_build, repair_build, check_build_prerequisites, remove_build, run_build, list_models, pin_model, refresh_models, inspect_model, verify_model, remove_model, create_build, download_model, cancel_job, subscribe, unsubscribe` **+ `compose_config` (new, coder in progress)**. Events: `phase, log, progress, ready, health, gpu, exited, job_progress, job_done, agent_error` (errors fold into phase/exited/health via `error_kind`). `typed_sidecar_resources` is a **capability flag**, not a method.

## I.6 TUI screens (`src/vela/tui/`)
`app.py` (dashboard) + screens: `config_picker, confirm, help, log_prompt, build_manager, create_build, adopt_build, flag_manager, model_manager, pin_model, download_model, target_manager, target_edit` **+ `new_deployment` (new, coder in progress)**. The **FlagManager** = the existing "edit any modeled/passthrough vLLM flag with live preview + soft‑validate + persist" surface (central to the composer UX discussion, Part VII/IX).

## I.7 Packaging / install model
Hatchling; deps huggingface‑hub, httpx, psutil, pydantic v2, PyYAML, rich, textual, tqdm, typer; extras `gpu`(nvidia‑ml‑py), `dev`(pytest, pytest‑asyncio, ruff). **vllm NOT a dep.** Per‑host dirs: configs `~/.config/vela/`, runs `~/.local/state/vela/runs`, builds `~/.local/share/vela/builds`, socket `$XDG_RUNTIME_DIR/vela/agent.sock`. **One package both sides;** agent = `vela agent connect`/`run`. SSH transport runs `PATH=<venv>/bin:$PATH … vela agent connect`. Optional `VELA_AGENT_TOKEN` for shared hosts.

---

# PART II — LAB INFRASTRUCTURE (verified via SSH; key `/Users/brennanconley/vibecode/infx/ubuntu24_ed25519`)

| Host | Role | Address | Notes |
|---|---|---|---|
| Mac | author | — | this repo; no GPU |
| **P620‑01** | controller | `bgconley@10.25.0.50` | zfs `/tank`; also runs the 32B lane |
| **Blackbird** | GPU agent | `bgconley@10.25.0.51` | **RTX PRO 6000 Blackwell Max‑Q (sm120)**; Qwen3.6 27B |

**Blackbird Docker images:** `vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046` (the pinned one; vLLM `0.20.2rc1.dev9+g01d4d1ad3`), `vllm/vllm-openai:cu130-nightly`, `:nightly`, `vllm-mxfp4-bw-sm120:20260510` (NVFP4/MXFP4), `voipmonitor/vllm:cu130`.
**Blackbird weights** (`/home/bgconley/models/`): `qwen36-27b-bf16` (**54 GB**), `qwen36-27b-fp8-rp6000`, `qwen36-dual-fp8-vlm`, `qwen36-35b-a3b-bf16`, `deepseek-r1-qwen32b-bf16`, `gemma4-31b-bf16`.
**P620 zfs `/tank`:** `repos/infx/qwen36-27b-test/` holds the hand‑written start/stop scripts (`start-qwen36-bf16-rp6000-blackbird.sh`, `…fp8…`) + run notes; `repos/vela` (the clone); `triton/venv-vllm/bin/vllm` (vLLM `0.19.1rc1.dev`, used only by the **620‑01 Qwen3‑32B‑FP8** config). Vela venv at `/home/bgconley/venvs/vela` on both lab hosts.
**Containers observed:** `qwen36-27b-fp8-kvfp8-rp6000-vllm-loader` (image b13d6e5; the FP8 lane), `qwen36-35b-a3b-bf16-server` (image b13d6e5).
**Ports:** Qwen3.6 FP8 `:18003`, BF16 `:18002`, 620‑01 32B `:8017`.
**Insight:** FP8 and BF16 share the **same engine image**; only weights (`Qwen/Qwen3.6-27B-FP8` vs `Qwen/Qwen3.6-27B`) + dtype flags differ. The zfs vLLM venv is a *different* model/host (32B FP8 on 620‑01).

---

# PART III — THE REVIEW ARC (5 rounds; FULL findings preserved)

**Method (every round):** establish git/test ground truth → fan out 4–7 **Sonnet 4.6** agents (read‑only, evidence‑based, scoped per domain) → **Opus 4.8 independently verifies** each load‑bearing claim by reading the cited code, **corrects agent over/under‑ratings**, and **synthesizes** a findings doc → append to `.wolf/memory.md`.

**Commit→round mapping (`c20d6c1..2542867`, 48 commits, newest first):**
- **Post‑v5:** `2542867` Harden agent token misconfiguration handling *(N5‑1 fix)*.
- **v5 window** `0034df1..8ebb3db`: `8ebb3db` fake‑child fix · `cb5dbe3` **Rename to Vela** · `a760a99` token generator+strength *(V3‑6)*.
- **V3 closure window** `1f473c7..0034df1`: `0034df1` explicit frame‑handler auth state *(V3‑5)* · `8affb7f` target SSH forwarding *(V3‑2)* · `d918da8` assert local deep‑verify blob hashes *(V3‑7)* · `d8e88ea`+`6fcf6b6` harden NDJSON read loop *(V3‑1)* · `e4f9fa9` reject malformed SSH option assignments *(V3‑3)* · `fb491f1` large NDJSON frames over subprocess *(N4 real test)*.
- **v3 window** `aa497e0..1f473c7`: the SSH‑option hardening wave (`a32d6b0,1f9ab66,43c9845,555f7cd,091aa69,b3e728f,f5a0201,e4a2b1f,1f473c7,0610560`), `d7d3949` authenticated handshake per stream, `2c949d1` NDJSON request validation, `fcadec9` typed sidecar resource identity *(Q10)*, `1047042` formalize build/download phases *(Q10)*, `936f327` uv flow+token auth, `6b4e4f3` **stream model deep‑verify hashing** *(N1 OOM fix)*, `9d97f8e` harden RPC error codes+frame coverage *(N2, N4)*, `0b3aad7` stabilize uv precheck TUI test *(N6)*, `927cd64` keep build form open *(N6)*, `ecc975d` restore TUI color + surface agent errors *(N5)*.
- **v2 window** `c20d6c1..aa497e0`: `8896463` harden RPC framing+prechecks *(P1,P2)* · `545aaeb` close build/model review gaps *(P3–P8)* · `64554cd` harden private file+signal *(Q3,Q4,Q5)* · `c13b38e` classify vLLM API auth health *(Q1)* · `4c575b4` reject adopted builds w/ mismatched versions *(Q2)* · `273beb1` preserve delimiters when redacting *(Q6)* · `15440be` build/model manager bindings *(Q7)* · `d5e5e67` repoint default build after active removal *(Q8)* · `4e33a0f` split health snapshot from readiness *(Q1/Q9)* · `c45e6c3` harden packaging+profile cache *(Q11,Q12)* · `3eb257b` classify subprocess bridge failures · Figma UI commits (`d784498,2e187ec,60893b6`) · `d3bfec6`/`aa497e0` report+surface malformed NDJSON frames *(N5)*.

## III.1 Findings v1 (baseline `c20d6c1`, 580 tests, ~83–86%) — the original punchlist
**Medium (P1–P9), all later CLOSED:**
- **P1** recycled‑PID `stop`/`kill` returned `-32000` not `-32002` (`TrackedProcessMismatch` not wrapped). *Fixed: local.py `_request_stop/kill_signal` catch → `TargetCallError("identity-verification-failed")`.*
- **P2** NDJSON `StreamReader` default 64 KB limit vs 2 MiB frames → large frames silently dropped (`stdio.py`, `socket.py`, `subprocess.py`). *Fixed: `limit=FRAME_STREAM_LIMIT` at all reader sites.*
- **P3** model download had no `0600 downloads/<entry_id>.log` via LogSink. *Fixed (545aaeb).*
- **P4** model layer didn't inject `HF_TOKEN` at spawn for gated repos. *Fixed.*
- **P5** `model verify --deep` was a no‑op for HF‑cache entries. *Fixed (real blob hashing).*
- **P6** no pre‑launch build integrity re‑check (`preflight.py` checked model/port/world‑size only). *Fixed: `check_build_launch_integrity`.*
- **P7** `BuildErrorKind` missed torch/CUDA/arch/compile‑OOM classes. *Fixed.*
- **P8** no stale‑`creating` startup sweep. *Fixed: `sweep_stale_creating_builds`.*
- **P9** registry RPC handlers ran `fcntl.flock` on the asyncio loop. *Fixed: wrapped in `to_thread`.*

**Low (Q1–Q12):** Q1 `/v1/models` 401 misclassified `HF_AUTH` + probe_loop exits on error_kind *(fixed → `API_KEY_AUTH` + recovery)*; Q2 adopt no version‑agreement *(fixed)*; Q3 runs dir not `0700` *(fixed)*; Q4 temp files not `0600` *(fixed: `_write_private_text_atomic`)*; Q5 `_signal_process_group` didn't catch `PermissionError` *(fixed)*; Q6 redaction `\S+` over‑consumed delimiters *(fixed: negated class)*; Q7 Model `Enter`‑select / Build `F`‑flags / Help `b m F` *(fixed)*; Q8 active‑default build remove refused vs repoint *(fixed)*; Q9 `feature-unavailable -32011` vs `-32601`, `subscribe all?`, single‑shot `health` *(fixed: all? + single‑shot)*; **Q10 typed sidecar fields/formal enums** *(fixed: `engine/job_phases.py` `BuildPhase`/`DownloadPhase` + typed sidecar identity)*; Q11 systemd `After=network.target` *(removed)*; Q12 `lru_cache` profile staleness *(fixed: mtime/size cache key + `clear_profile_caches`)*.

## III.2 Findings v2 (`aa497e0`, ~627–633 tests flaky, ~89–91%) — bugs the coder then fixed
- **N1 (High) deep‑verify OOM:** `model_registry.py:1468` `data = path.read_bytes()` materialized whole multi‑GB shards. *Fixed (6b4e4f3): `_stream_file_sha256_uri` chunks at `HASH_CHUNK_BYTES`; **Opus‑verified the chunked digest is byte‑identical to the old whole‑file version** (name\0 size\0 content \0 order; `stat().st_size==len(data)`) → no integrity regression.*
- **N2 (Low‑Med)** `build-integrity-failed`/`cancelled`/`profile-error` not in the RPC code map → `-32000`. *Fixed (9d97f8e): `-32014/-32015/-32016`.*
- **N3 (Low)** ruff regressed (`transport/socket.py:1 I001`). *Fixed.*
- **N4 (Low)** P2 large‑frame test was hollow (asserted the `limit=` kwarg, not a real >64 KB round‑trip). *Fixed (fb491f1, 9d97f8e): 1 MiB through a real `StreamReader`.*
- **N5 (Low‑Med)** client‑side malformed frame `_fail_pending` nukes all pending RPCs (design judgment, asymmetric‑by‑design vs the server). *Surfaced via `agent_error` (ecc975d).*
- **N6 (Med) flaky TUI test** `test_build_manager_keeps_create_form_open_on_uv_precheck_failure` — passed in isolation, failed intermittently in the full suite (Textual/asyncio timing). *Fixed (0b3aad7, 927cd64): `_wait_for_textual_condition` (`pilot.pause()` loop) + an `asyncio.Event` sentinel; suite now deterministically green.*
- **Opus corrections:** the agent's **High "pip `--python` invalid → fallback broken"** was wrong (`pip --python` valid since pip 23.1) → Low/runtime‑unverified; the **High "sidecar TOCTOU skips SIGKILL"** is the anti‑PID‑reuse guard *working* (not a bug).

## III.3 Findings v3 (`1f473c7`, 746 deterministic, ~95–96% agent‑arch) — security wave + V3 items
The SSH/auth hardening wave landed (option‑injection defense + per‑stream token auth + NDJSON validation). New (all Low, later closed): **V3‑1** read‑loop didn't catch oversized‑line `ValueError`/`RecursionError` (per‑connection self‑DoS) *(fixed 6fcf6b6/d8e88ea)*; **V3‑2** `-A`/`-o *Forward` not blocked *(fixed 8affb7f)*; **V3‑3** `-o=Key=Value` empty key passed validator *(fixed e4f9fa9)*; **V3‑4** non‑`TargetCallError` mid‑handshake leaves connection unauthenticated until reconnect (fail‑closed) *(partially addressed by 2542867)*; **V3‑5** `_handle_frame(auth_state=None)` default bypass *(fixed 0034df1)*; **V3‑6** no token‑gen utility/entropy floor *(fixed a760a99)*; **V3‑7** local‑model deep‑verify test asserted file names not hash values *(fixed d918da8)*. **Opus corrections:** dismissed an agent "probe_loop never exits" claim (it's the FR‑18 DEGRADED↔READY recovery, by design); the redaction greedy regex is the spec‑mandated over‑mask (safe — never under‑masks). **SSH filter is a hybrid allow/denylist** (unknown bare flags rejected; dangerous `-o` keys denied; value‑conditional for TTY/stdio/host‑verification) — reviewers found **no bypass**.

## III.4 Findings v4 (`1f473c7`, docs review) — the original punchlist + doc accuracy
Original v1 punchlist **CLOSED** (`discover_runs_no_paths` now dispatched, `local.py:392`). Docs **substantially accurate & in‑sync** with the live SSH commits. Drift (Low‑Med): **D1** README `vela tui` didn't exist → **fixed** (`@app.command("tui")` + README); **D2** `configuration.md` `inprocess`→`in_process` *(OPEN — doc typo)*; **D3** `builds-and-models.md` build‑vs‑model `--force` ambiguity (build remove has no `--force`) *(OPEN — doc)*; **D4** `error` listed as a discrete event but folded into phase/exited/health; **D5** SSH rejection list accurate but incomplete. **Opus correction:** `typed_sidecar_resources` is a **capability flag**, not an undispatchable method (agent misread; dismissed).

## III.5 Findings v5 (`8ebb3db`, "finished" claim + rename) — last bugs
Validated the rename (Part IV) and confirmed V3‑1..V3‑7 + N4 + V3‑6 + D1 closed. New: **N5‑1 (Med)** misconfigured `VELA_AGENT_TOKEN` silently drops every connection — `_ConnectionAuthState()` built **outside** the `try` in `agent/stdio.py serve_agent_stream`, and `agent/socket.py handle_connection` has no `except`, so `AgentTokenError` killed the connection task with only an asyncio log. **N5‑2 (Low)** token strength is length‑only (a 22‑char `"aaaa…"` passes despite the "128 bits" message). **N5‑3 (Low)** redundant min‑check in `gen-token`. *N5‑1 addressed at HEAD `2542867` "Harden agent token misconfiguration handling" — the read loop now catches `ValueError` and the token path was hardened; **verify the exact mechanism** (it surfaces a clear error rather than dropping).* D2/D3 (docs) and N5‑2 (Low) remain.

## III.6 Tests / CI / artifacts (verified authentic)
749 collected, deterministically green; pytest‑asyncio first‑run flake in ~3 tests is a harness artifact (warm‑pass). `.github/workflows/remote-validation.yml` is a genuine **self‑hosted** lane (P620 runner; daily cron + manual `fast|full`; concurrency guard; `gha-{run_id}-{run_attempt}` build labels). `artifacts/remote-validation/*.md` are **authentic** (real Actions run URL `…/actions/runs/26976430928`, NVML UUIDs, multi‑GB wheel sizes, random ports, inode numbers, ULIDs, `GATED_MODEL_AUTH_OK`). `tests/test_branding.py` guards the rename; `tests/test_docs.py` gates doc sections.

---

# PART IV — THE VELA RENAME (validated, v5)
Complete + correct + consistent (commit `cb5dbe3`):
- **Structural:** `src/vela/`, `name=vela`, `vela=vela.cli:main`, `packages=["src/vela"]`; old package gone; imports/entrypoint resolve.
- **No residual old name** in any *live* surface (only `test_branding.py`'s forbidden list + historical artifacts/punchlists/my‑findings retain it; `scripts/fake_vllm_child.py` → `from vela.fake_child`).
- **Upstream vLLM preserved** (not over‑renamed): `import vllm`, `vllm serve`, `VllmProfile`×28, `wheels.vllm.ai`, `vllm==`.
- **Consistent:** `~/.local/state/vela`, `~/.config/vela`, `~/.local/share/vela/builds`, socket; env `VELA_*` (code + `scripts/run_remote_tests.sh` + `.github/workflows/remote-validation.yml`); `vela-agent.service`; handshake `vela_version`; `DEFAULT_AGENT_COMMAND=("vela","agent","connect")`; ControlPath `~/.ssh/vela-%C`.
- **Self‑guarding:** `tests/test_branding.py` (4 tests: pyproject branding; runtime paths/constants; CLI `--help` branded + 7 old names absent; live docs/scripts old names absent).

---

# PART V — OPERATIONAL: INSTALL & RUN

**Model:** Python package (pip/pipx), not a binary; `vela` console script; **same package both sides**; vLLM provided via 4‑level precedence `command.executable > command.build > global default build > bare vllm on PATH`.

**Blackbird (target):** `git clone https://github.com/bgconley/vela.git ~/repos/vela && cd ~/repos/vela && python3 -m venv ~/venvs/vela && ~/venvs/vela/bin/pip install ".[gpu]"`. Provide vLLM via `vela build add …` (managed venv; nightly/commit need `uv`), `vela build adopt <venv>`, or a Docker wrapper/image (the lab's path).
**P620 (controller):** clone + venv + `pip install .`; `vela targets add blackbird --host bgconley@10.25.0.51 --venv /home/bgconley/venvs/vela --workdir /home/bgconley/repos/vela`; `vela targets test blackbird`; `vela --target blackbird`.
**Gotchas (→ onboarding spec):** target `--venv` must match the install (no pipx on SSH targets); configs live on the **target**; nightly/commit need `uv`; SSH must be passwordless (BatchMode); per‑host dirs; token auth optional.

---

# PART VI — DEPLOYMENT CREATED THIS SESSION (Qwen3.6 27B BF16)

**Q:** which vLLM build for Qwen3.6 27B **BF16** on Blackbird? **A (via SSH):** the Docker image `vllm/vllm-openai@sha256:b13d6e5…` (vLLM 0.20.2rc1.dev9), weights `/home/bgconley/models/qwen36-27b-bf16` (54 GB), defined by `/tank/repos/infx/qwen36-27b-test/start-qwen36-bf16-rp6000-blackbird.sh`. **No vela config existed** (only FP8). Created two files (real, validated on Blackbird; **uncommitted** + scp'd to Blackbird):

**`configs/qwen36-27b-bf16-rp6000-blackbird.yaml` (verbatim):**
```yaml
name: qwen36-27b-bf16-rp6000-blackbird
description: Blackbird RTX PRO 6000 real TUI config for Qwen/Qwen3.6-27B (BF16, kv bf16).
model: Qwen/Qwen3.6-27B
served_model_name: qwen36-27b-bf16-rp6000
command: { entrypoint: serve, executable: ./scripts/blackbird_qwen36_bf16_vllm_foreground.sh }
engine: { gpu_memory_utilization: 0.95, max_model_len: 262144, dtype: bfloat16, kv_cache_dtype: bfloat16, max_num_seqs: 4 }
server: { host: 0.0.0.0, port: 18002, exposure: lan, api_key: EMPTY }
logging: { request_logging: false, suppress_access_log_for: [/health] }
env: { CONTAINER: qwen36-27b-bf16-rp6000-vela, ROOT: /home/bgconley/models/qwen36-27b-bf16, HF_CACHE_ROOT: /home/bgconley/models/qwen36-27b-bf16/hf-cache, PULL_IMAGE: "0" }
extra_args: [--max-num-batched-tokens, "8192", --trust-remote-code, --language-model-only, --enable-prefix-caching, --enable-auto-tool-choice, --reasoning-parser, qwen3, --tool-call-parser, qwen3_coder]
launch: { mode: attached, ready_timeout_seconds: 1800, health: {interval_seconds: 2}, runs_dir: /home/bgconley/models/qwen36-27b-bf16/vela-runs }
vllm: { version_profile: "0.11" }
```
**`scripts/blackbird_qwen36_bf16_vllm_foreground.sh`** — foreground Docker launcher mirroring the FP8 one's supervision (`docker run -d` + `docker logs -f` + `docker wait` + signal‑stop cleanup), BF16 defaults, image b13d6e5, container `qwen36-27b-bf16-rp6000-vela`, mounts `ROOT:ROOT`, evicts sibling Qwen3.6 containers, and **omits `--kv-cache-memory-bytes`** (the FP8 wrapper pins 60 GB — correct for FP8 weights but would **OOM** the RP6000 with 2×‑larger BF16 weights; KV sized by `gpu_memory_utilization 0.95` instead — **the key footgun**).
**Validated:** `vela list` shows it; `vela preview` resolves the exact command (does not launch); weights present (54 GB). Differences from FP8: model `Qwen/Qwen3.6-27B` (vs `-FP8`), port `18002` (vs 18003), container name, `dtype bfloat16`/`kv bfloat16`, no KV pin; the two evict each other (RP6000 fits one).

---

# PART VII — FORWARD‑LOOKING SPECS (this session, repo root)
Research grounding via Exa (official vLLM Docker docs + 2026 production guides: `--ipc=host`/`--shm-size` 16g/32g, HF‑cache volume, `HF_TOKEN`, `--gpus`, digest‑pin not `:latest`, `/health`+`/v1/models` generous start‑period, graceful SIGTERM, `--restart no` since the manager supervises).

- **`vela-onboarding-ux-spec-v1.md`** — `vela targets bootstrap` (probe SSH → install/locate vela → auto‑resolve agent path, killing the `--venv` footgun → optional default build → write target → handshake test) + `vela doctor` (introspect both hosts; name every failure's fix). R1 auto‑resolve agent (`target.agent_command`), R2 `config push/pull/lint` (realizes `push_config` §10.3), R3 build doctor/uv preflight, R4 SSH `setup-ssh`/key + stderr surfacing, R5 per‑host path reporting, R6 token‑as‑file + the N5‑1 loud‑fail. Priority P0 = fix N5‑1 + R1 + named‑failure remediations.
- **`vela-deployment-composer-user-stories-v1.md`** — 6 epics, ~25 stories with acceptance criteria; MVP cut.
- **`vela-deployment-composer-spec-v1.md`** — agent‑side `ComposerService`; RPCs `compose_config`, `suggest_deployment_defaults`, `allocate_port`, `validate_config`, `save_config`, `clone_config`, `delete_config`, `list_presets`, `config push/pull/lint`; auto‑derivation (served‑name, free port in 18000–18999, run‑dir, exposure, container name); engine presets (`balanced/throughput/long-context/low-memory/qwen3-text`) + per‑model suggestions; TUI "New Deployment" flow; `vela deploy create/edit/clone/list/delete`. Phases **DC0–DC5**.
- **`vela-docker-runtime-spec-v1.md`** — `command.runtime: process|docker` + `command.docker`; agent `DockerRuntime` backend (`build_docker_run` with best‑practice defaults; container identity = name+id+image digest; verify‑before‑act → `docker stop/kill`; logs via LogSink; health/FSM unchanged; `DockerErrorKind`). Codifies the lab's proven wrapper shape. Phases **DK0–DK4**. **DK0 pins:** the `serve`‑strip rule (image entrypoint already runs `vllm serve` → pass `<model‑positional> <flags>`) and the **FP8‑vs‑BF16 KV footgun** as test assertions.
- **`vela-docker-runtime-examples-v1.md`** — the DK4 anchor: both Blackbird wrappers converted to native `runtime: docker` configs + the exact `docker run` each must generate + acceptance hooks (kept out of `configs/` until the schema lands).
- **`vela-deployment-composer-implementation-plan-v1.md`** — combined **DK+DC** plan: shared pre‑work (S1 schema discriminator, S2 error codes `-32018..-32022`, S3 fake‑docker harness), dependency graph (interleave `DK0→DC0→DK1→DC1→DC2→DK2→DC3→DK3→DC4→DK4→DC5`), reuse map, risks, DoD.
- **Open UX decision (raised by the user):** the composer is ~90% backend; almost all UX exists (managers + **FlagManager = "customize any flag"**). Recommendation: **reuse‑first** — one small "New Deployment" collector + sequence the existing modals + a Save action, **not** a bespoke 6‑step wizard. *DC4 should be revised to say this (not yet folded into the spec file).*

---

# PART XI — NEW CAPABILITIES IN ACTIVE DEVELOPMENT (uncommitted, read from the working tree)

The coder is **implementing the deployment‑composer + docker‑runtime specs right now** — present in the working tree, **not yet in any commit**. This section documents *exactly what exists today* (read directly from the source), what it does, the design choices made, and what remains versus the specs. Re‑read these files next session; they change live.

**Working‑tree changes (uncommitted):**
- **Modified:** `config/schema.py` (`RuntimeKind` enum @21; `DockerConfig` @35; `command.runtime`/`command.docker` @64/69; mutual‑exclusion validator @75), `transport/rpc_errors.py` (`-32018 image-not-found` … `-32022 config-exists`), `agent/local.py` (dispatches `compose_config` @381/646), `engine/command_builder.py`, `tui/app.py`, `tui/screens/help.py`, tests (`test_command_builder.py`, `test_config_loader.py`, `test_tui_smoke.py`).
- **New (untracked):** `engine/composer.py` (356 ln), `engine/docker_runtime.py` (85 ln), `tui/screens/new_deployment.py` (187 ln), `tests/test_deployment_composer.py`.

## XI.1 The Deployment Composer — `engine/composer.py` (DC0/DC1 substantially done)
Pure, agent‑side, no I/O beyond reading the config registry. Public surface:
- **`ComposeResult`** dataclass: `{config: ModelConfig, warnings: list[str], derived: list[{field,value,source}]}`.
- **`PRESETS`** (5, as data): `balanced` (gpu_mem 0.90, dtype auto, `--enable-prefix-caching`), `throughput` (0.92, max_num_seqs 32), `long-context` (0.90, max_num_seqs 4), `low-memory` (0.85, max_num_seqs 2), `qwen3-text` (`--language-model-only --reasoning-parser qwen3 --tool-call-parser qwen3_coder`, `applies_to qwen/qwen3`). Exposed via **`list_presets()`**.
- **`compose_config(spec, *, configs_dir)`** — the pipeline: `name` ← slug of `spec.name` or `model_basename(model)`; `served_model_name` ← `model_basename`; `port` ← `allocate_port`; `runs_dir` ← `default_run_artifacts_dir()/name`; `exposure` ← `local`; `command` ← `_runtime_command` (process: `executable` or `build`; **docker: `{image, container_name: vela-<name>}`**); `engine`/`extra_args` seeded from the preset; then `_merge_overrides` (engine/server/launch/env) + `_merge_extra_args` (append). Validates via `ModelConfig.model_validate`. Returns derived‑fields provenance (served_model_name/port/runs_dir/container_name).
- **`allocate_port(*, preferred, configs_dir, port_range=(18000,18999))`** — scans **configured** ports (`load_registry().valid`), honors a free `preferred` else returns the lowest free + a `port-reassigned` warning. *Gap vs spec: does NOT yet scan live sidecars / `ss` listeners / `docker ps` — configured‑ports only.*
- **`suggest_deployment_defaults(params, *, configs_dir)`** — served‑name/port/runs_dir/exposure + (docker) `container_name`. *Gap: `engine_suggestions` is `{}` — no per‑model dtype/kv/TP inference from `config.json`/registry yet.*
- **`validate_config_payload(payload)`** — Pydantic validation → `_lint_config` (host‑local absolute `model`/`executable`/`cwd` → portability warnings) → `build_command(cfg, select_profile_for_config(cfg))` soft‑validate; returns `{ok, errors[], warnings[]}`. *Gap: lint doesn't yet block secret literals.*

## XI.2 The Docker Runtime generator — `engine/docker_runtime.py` (DK0 done; DK1+ NOT yet)
- **`build_docker_run(cfg, resolved_serve_args, env, *, docker_binary="docker") -> DockerRunCommand{argv, env, metadata}`** — emits `docker run --name <container_name|vela-cfg.name> [--gpus <g>] [--network <n>] [--ipc=host] [--shm-size <s>] [--restart <r>] [--entrypoint <e>] (-e KEY)… (-v VOL)… <extra_run_args> <image> <serve args>`.
- **The serve‑strip rule IS implemented** (`_container_serve_args` drops a leading `serve` — the image entrypoint already runs `vllm serve`).
- **`_default_shm_size`** = `32g` if `tensor_parallel_size>1` else `16g` (matches the spec / best practice).
- **Volumes** (`_docker_volumes`) = `docker.volumes` + (if `hf_cache` set) `hf_cache:hf_cache_target:rw`.
- **Secret‑safe env pattern:** env is emitted as `-e KEY` (name only — Docker passes the value from the run process's environment); the *values* live in `DockerRunCommand.env` (kept out of argv → never logged). The caller must apply `DockerRunCommand.env` to the `docker run` subprocess env.
- **What's MISSING (this is a pure generator, not the runtime):** no image‑digest resolution/recording, no `docker run -d`/`logs -f`/`wait` supervision, no container‑identity sidecar, no verify‑before‑`docker stop/kill`, no `DockerErrorKind`, no eviction/port‑guard, no discover/reattach. **DK1–DK4 are not built yet.** (Today, docker deployments still run via the `command.executable` wrappers — Part VI.)

## XI.3 The schema — `config/schema.py`
`RuntimeKind(str, Enum) {PROCESS, DOCKER}`; `CommandConfig.runtime` (default PROCESS); `CommandConfig.docker: DockerConfig | None`; `DockerConfig{image, container_name?, gpus="all", ipc_host=True, shm_size?, network="host", volumes[], hf_cache?, hf_cache_target="/root/.cache/huggingface", env{}, restart="no", stop_grace_seconds=90, entrypoint?, pull="never|missing|always", evict[], extra_run_args[]}`; validator @75 enforces docker XOR executable/build and requires `image`.

## XI.4 RPC + error codes — `agent/local.py`, `transport/rpc_errors.py`
`compose_config` is dispatched (`local.py:381` → `_compose_config` @646 → `composer.compose_config(params, configs_dir=…)`). Error codes `-32018 image-not-found`, `-32019 name-conflict`, `-32020 daemon-unreachable`, `-32021 compose-invalid`, `-32022 config-exists` are present. *Not yet seen wired: `save_config`/`clone_config`/`delete_config`/`suggest_deployment_defaults`/`list_presets`/`allocate_port` as RPCs (DC2); and `validate_config` as an RPC.*

## XI.5 The TUI — `tui/screens/new_deployment.py` (reuse‑first, NOT a 6‑step wizard ✔)
A single `ModalScreen[dict|None]` **collector form** (76 cols): **Name**, **Runtime** Select(process/docker), **Model**, **Docker image**, **Preset** Select (from `list_presets`), **Host** (default 127.0.0.1), **Port** ("auto"), **Exposure** Select(local/lan/public). `Ctrl+S` → `_collect_spec()` builds the composer spec dict (`{name,target,model,preset,runtime,overrides:{server:{host,exposure,port?}}}`; docker → `{kind:docker,image}`) and `dismiss`es it; `Esc` cancels. **This is exactly the reuse‑first lean form recommended (Part IX), not a bespoke multi‑step wizard.** *The app wiring (collect → `compose_config` → preview/preflight → `save_config` → smoke) and the `n` binding/palette entry need confirming in `tui/app.py`.*

## XI.6 Status vs the specs — DONE / PARTIAL / NOT‑YET
- **DONE:** S1 schema discriminator; S2 error codes; DC0 (compose/derive/presets/validate+lint); DK0 (`build_docker_run` generation incl. serve‑strip + shm default); DC4‑lite (the collector screen); `compose_config` RPC.
- **PARTIAL:** DC1 — `allocate_port` (configured‑ports only; add live sidecars/`ss`/`docker ps`) and `suggest_deployment_defaults` (`engine_suggestions` empty; add per‑model dtype/kv/TP from `config.json`).
- **NOT YET:** **DK1–DK4** (the entire docker *lifecycle/identity/supervision/validation* — the generator exists but nothing launches/monitors/stops a container natively); DC2 (`save_config`/`clone`/`delete` RPCs + `config push/pull/lint`); DC3 (`vela deploy` CLI); the wizard→save→smoke app wiring; DK0 BF16‑KV‑footgun test assertion; the onboarding spec (`targets bootstrap`/`doctor`).

## XI.7 What a future session should do here
1. `git status`/`git diff` to see the coder's *current* progress (this is a snapshot).
2. Review the above files against the specs (Part VII); confirm `build_docker_run` keeps the serve‑strip and that a BF16 native‑docker config does **not** acquire an FP8 KV pin.
3. Drive **DK1** next (it's the highest‑value gap — without it, `runtime: docker` configs can't actually launch; they still need the wrapper).
4. Add the `allocate_port` live scans and the per‑model `engine_suggestions`.
5. Wire the collector screen end‑to‑end (compose → preview → preflight → save → optional smoke) + the `n` binding.
6. When DK1+ land, validate the native‑docker FP8/BF16 on Blackbird (DK4) and retire `scripts/blackbird_qwen36_*_vllm_foreground.sh`.
**Do not commit on the coder's behalf; coordinate.**

---

# PART IX — OPEN ITEMS / NEXT STEPS
1. **N5‑1** — verify the `2542867` fix surfaces a clear error (not a silent drop) for a malformed token; **N5‑2** length‑only token strength still Low‑open.
2. **D2** `configuration.md` `inprocess`→`in_process`; **D3** build‑vs‑model `--force` doc clarity — both Low/doc, still open.
3. **Forward features (in progress, Part XI)** — review the coder's composer/docker‑runtime work against the specs; land the DC4 reuse‑first UX; DK4 real‑Blackbird validation of native‑docker FP8/BF16; then retire `scripts/blackbird_qwen36_*_vllm_foreground.sh`.
4. **Operational** — commit the BF16 config+wrapper when satisfied; optionally `vela smoke-tui qwen36-27b-bf16-rp6000-blackbird` to confirm READY on `:18002`.
5. Optional: migrate ~77 remaining TUI tests off the fixed‑sleep helper to `pilot.pause()`.

---

# PART X — KEY DECISIONS & LEARNINGS
- **Independent Opus verification is the value, not agent relay** — several Sonnet findings were downgraded/dismissed each round on direct code reads (pip‑`--python` validity; "sidecar TOCTOU"; `typed_sidecar_resources`; "probe_loop never exits"); and real High/Med bugs the agents' green runs missed were caught by deterministic re‑runs (N1 OOM; the order‑flaky N6; N5‑1).
- **`feature-unavailable` sites are catch‑all guards, not stubs** — a recurring false alarm; the real install/download paths are genuine subprocesses.
- **Docker is the lab's real runtime for Qwen3.6 27B;** FP8/BF16 share one engine image (weights+dtype differ); the FP8 KV‑memory pin must NOT carry to BF16 (OOM) — now encoded as a DK0 test assertion.
- **The composer's value is backend; the UX mostly exists** (FlagManager = "customize any flag") → reuse‑first, no bespoke wizard.
- **Rename hygiene:** app name (`vela`) ≠ upstream tool (`vLLM`, preserved); a branding guard test makes it self‑policing.
- **Git discipline:** the reviewer commits nothing; the coder commits live (HEAD moved 3× during one review round, and the tree now holds uncommitted forward‑feature work) — always re‑establish ground truth.

---

# APPENDIX — QUICK REFERENCE
**Hosts:** controller `bgconley@10.25.0.50` (P620‑01) · GPU agent `bgconley@10.25.0.51` (Blackbird, RTX PRO 6000 sm120). SSH key `/Users/brennanconley/vibecode/infx/ubuntu24_ed25519`.
**Engine image:** `vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046` (vLLM 0.20.2rc1.dev9). **Ports:** FP8 `:18003`, BF16 `:18002`, 32B `:8017`.
**RPC error codes:** `-32700/-32600/-32601/-32602` (JSON‑RPC), `-32000` internal, `-32001` run‑not‑found, **`-32002` identity‑verification‑failed**, `-32003` not‑stoppable, `-32004` config‑not‑found, `-32005` preflight‑failed, `-32006` version‑mismatch, `-32007` build‑not‑found, `-32008` model‑not‑found, `-32009` resource‑in‑use, `-32010` job‑already‑running, `-32011` feature‑unavailable, `-32012` agent‑unreachable, `-32013` command‑not‑found, `-32014` build‑integrity‑failed, `-32015` cancelled, `-32016` profile‑error, `-32017` agent‑auth‑required, **`-32018` image‑not‑found, `-32019` name‑conflict, `-32020` daemon‑unreachable, `-32021` compose‑invalid, `-32022` config‑exists** (last five = composer/docker, coder in progress).
**vLLM Docker recipe (research‑confirmed):** `docker run --gpus all --ipc=host (or --shm-size 16g/32g) -v <hf-cache>:/root/.cache/huggingface -e HF_TOKEN -p <port> --restart no <digest-pinned image> <model> <serve flags>`.
**Commands:** `vela list|preview|run|smoke|smoke-tui|tui` · `vela targets add|test|list` · `vela build add|adopt|select|verify|list` · `vela model pin|download|verify|list` · `vela agent start|status|connect|gen-token` · `vela --target <name>` · *(in progress)* `vela deploy create`.
**Resume checklist (next session):** `git -C ~/vibecode/lab-tui status` (expect the coder's uncommitted forward work) + `git log --oneline -15`; `PYTHONPATH=src python -m pytest -q`; `ruff check .`; crown‑jewel grep on `tui/app.py`+`cli.py`; read `engine/composer.py`/`docker_runtime.py`/`new_deployment.py`; `ls *.md` for the artifact set (5 review findings + 6 forward specs + this doc).

*Snapshot as of 2026‑06‑06. The codebase is a live‑moving target with uncommitted in‑progress work — re‑establish git/test ground truth before acting.*
