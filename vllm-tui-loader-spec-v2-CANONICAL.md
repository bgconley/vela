# vLLM TUI Model Loader — Canonical Specification & Implementation Plan (v2)

**Binary:** `vllm-loader` · **Status:** canonical v2 — implementation-ready · **Audience:** the engineer(s) who will build and maintain it.

> **This document supersedes and replaces** the v1 spec, the v2 delta, and the v2 rev-B corrections. It is self-contained: do not cross-reference the older layers. Where this document and any earlier layer disagree, **this document wins.**

-----

## 0. Document status & changelog

This canonical v2 integrates four rounds of design and review. The design is converged. Relative to the original v1 draft, the following were **actively changed or removed** (listed so reviewers can confirm the stale content is gone):

- **Removed:** the “leave it running” option when quitting in attached mode (runtime detach of a PTY-bound child is unsafe; survivability now requires launching in detached mode — §7.4).
- **Changed:** “the full raw log is persisted” → the durable log holds **scrubbed, newline-committed** records only (transient progress frames are not persisted), at `0600` (§7.5).
- **Changed:** `RichLog(markup=True, highlight=True)` → `RichLog(markup=False, highlight=False)` writing `rich.text.Text` (raw logs must not be parsed as markup — §8.4).
- **Changed:** example `server.host: 0.0.0.0` → `127.0.0.1` default + an `exposure` model and a network-exposure warning (§7.9).
- **Changed:** assumed stdout/stderr source tagging → the MVP **merged-PTY** path does not separate streams; two-PTY/wrapper is an explicit option (§7.5).
- **Added:** phases `RESOLVING_MODEL`, `DOWNLOADING_MODEL`, `DEGRADED`, and error kind `HF_AUTH` (§7.6).
- **Added:** the `VllmProfile` version-adapter (flag spellings, **defaults table**, known enum sets, log-pattern packs) and the unifying flag-emission rule (§7.2–§7.3).
- **Changed:** schema fields that map to vLLM no longer carry baked defaults; they default to **unset** (§7.1). Fast-evolving enums (`kv_cache_dtype`, `quantization`, `load_format`) are open `str` validated softly through the profile.
- **Changed:** health probe hits `/health` **unauthenticated** and `/v1/models` with **Bearer** when a key is set (§7.7).
- **Added:** PTY capture for attached mode (EIO-as-EOF, incremental decode-then-split, close-slave-in-parent, fixed PTY width), a scrubbing log sink with a **bounded partial-line buffer**, a detached **supervisor** with its own identity verification, and a **run manifest** for log rotation (§7.5, §7.10).
- **Changed:** roadmap from “8–14 days” to **~1 week MVP / ~2–3 weeks polished v1** with an explicit cut (§15).

All factual claims about vLLM and Textual in this document were verified against current documentation (Appendix B). Because vLLM’s flag spellings, defaults, and log strings drift between releases, version-specific knowledge is isolated to the `engine` package and the `VllmProfile` (§7.2) so upgrades touch one place.

-----

## 1. Vision & design philosophy

### 1.1 The problem

Bringing up a vLLM server in a lab today means remembering a long `vllm serve …` command, running it in a terminal, and squinting at a fast-scrolling wall of logs to answer three questions: *is it loading or stuck? where in the load is it? is it ready, and on what URL/model name?* Configurations drift between people and runs, with no single artifact that is “the config for model X.”

This application turns that into: **pick a named config → press one key → watch a clean, phase-aware view of the load → get a clear “READY on http://… as model-name” signal.**

### 1.2 “Feels like Claude Code,” decomposed into testable properties

- **Single-keystroke primary actions** (load, stop, restart), shown in a persistent footer.
- **Always-visible context**: a header/status line always answers what is loaded, in what state, where.
- **Streaming, legible output**, colorized by severity, with the *current phase* surfaced separately from the raw firehose.
- **Discoverability via a fuzzy command palette** (`Ctrl+P`), so users don’t have to learn shortcuts to be productive (Textual provides this natively).
- **Minimal but informative chrome**; status by color + icon, not noise.
- **Never blocks**: the UI stays responsive regardless of engine activity (an architectural guarantee — §6.2).
- **Graceful, named errors**: failures (OOM, port-in-use, bad path, gated model) are detected, named in plain language, and surfaced as a banner with the relevant log excerpt.
- **Low-friction defaults**: the only required input is which config to load.

### 1.3 Design non-goals (v1)

Not a chat client/playground; not a cluster scheduler/multi-node orchestrator; not a replacement for `systemd`/Kubernetes as a long-term supervisor (though detached “leave it running” is supported — §7.4); not a config-authoring GUI (v1 reads, validates, and displays configs; editing is a fast-follow — §16).

-----

## 2. Elemental concepts (the shared mental model)

### 2.1 What vLLM is, and what “loading a model” entails

**vLLM** is a high-throughput LLM inference/serving engine. Its KV-cache memory is managed with **PagedAttention** in fixed-size GPU **blocks**. For serving it exposes an **OpenAI-compatible HTTP API** (`/v1/chat/completions`, `/v1/models`, …) plus a `/health` endpoint, launched via `vllm serve <model> [flags]`.

A load is a sequence of observable steps; each becomes a **phase** in our UI:

|Phase                                  |What vLLM does                                                                          |Why it can be slow                                                       |
|---------------------------------------|----------------------------------------------------------------------------------------|-------------------------------------------------------------------------|
|`STARTING`                             |Spawns; parses config; initializes engine, parallel/distributed state, attention backend|Import + CUDA init; usually seconds                                      |
|`RESOLVING_MODEL` *(HF only)*          |Resolves a Hugging Face repo id / snapshot                                              |Network/metadata; gated-repo auth can fail here                          |
|`DOWNLOADING_MODEL` *(cache miss only)*|Downloads model files from HF                                                           |Tens of GB on first run; skipped if cached                               |
|`LOADING_WEIGHTS`                      |Reads weights (often `*.safetensors`), shards across GPUs, moves to device              |I/O-bound; emits a shard progress bar                                    |
|`PROFILING_KV`                         |Profiles memory; computes KV-cache blocks / max concurrency                             |Runs a forward pass; depends on `gpu_memory_utilization`, `max_model_len`|
|`CAPTURING_GRAPHS`                     |Captures CUDA graphs for batch shapes (skipped if `enforce_eager`)                      |Seconds; emits its own progress bar                                      |
|`SERVER_STARTING`                      |Registers routes; starts Uvicorn on host:port                                           |Quick                                                                    |
|`READY`                                |`/health` returns 200; model is servable                                                |—                                                                        |
|`DEGRADED`                             |Process alive but `/health` failing after having been READY                             |—                                                                        |

`RESOLVING_MODEL`/`DOWNLOADING_MODEL` are **best-effort hints** (a cached or local model skips them; download and load can interleave). Two parameters dominate success and are shown prominently: **`tensor_parallel_size`** (how many GPUs the model shards across; `pp*tp` must fit available GPUs) and **`gpu_memory_utilization`** (fraction 0–1 of each GPU vLLM may use; too high → OOM, too low → tiny KV cache).

### 2.2 Why Textual

A TUI needs an event loop, widgets, layout, focus/keyboard handling, styling, and ideally mouse + multiple screens. **Rich** renders beautifully but is not an app framework. **Textual** (by the Rich authors) is async-native (asyncio), has a CSS-like styling system (TCSS), a real widget library (`RichLog`, `DataTable`, `ProgressBar`, `Input`, …), reactive state, background **workers**, multiple **screens**, a built-in **command palette** (`Ctrl+P`), responsive breakpoints, and can be served to a browser (`textual serve`). It directly delivers the §1.2 properties with the least custom plumbing.

### 2.3 Why we run vLLM as a subprocess, not in-process

We **spawn `vllm serve` and monitor it**, rather than `import vllm` in our process, because: (1) it isolates CUDA + multi-GB weight loading from the UI loop (responsiveness); (2) it matches how vLLM is actually run; (3) it gives lifecycle independence (stop/restart/detach without restarting the TUI); (4) the child’s stdout/stderr is exactly the stream we display; (5) the TUI holds **no GPU memory** and can run over SSH / on a login node. The cost: we observe the engine through its output and HTTP endpoints, hence the log-parsing FSM (§7.6) and health probes (§7.7).

### 2.4 The carriage-return / progress-bar problem

vLLM’s logging and progress bars write primarily to **stderr**, and the weight-shard loader and CUDA-graph capture emit **`tqdm`-style bars** that update in place with a **carriage return (`\r`)**, not a newline. Naive `readline()` (which only returns on `\n`) makes the UI look frozen during exactly the slow steps. So the reader splits on **both `\r` and `\n`** (transient on `\r`, committed on `\n`). Additionally, these libraries detect whether they are attached to a terminal and **degrade progress rendering when output is a plain pipe/file** — which is why attached mode captures the child under a **PTY** (§7.4). This affects only the animated bars; the discrete `INFO …` log lines arrive fine regardless, so the phase FSM works in both modes.

### 2.5 Logs are hints; health proves liveness; the FSM is authoritative

The load is a finite state machine. Logs **advance** a visible phase (hint-level); process exit **terminates** state; health probes **prove** readiness and ongoing liveness; timeouts **produce diagnostics**. Loading phases are monotonic; after `READY` the lifecycle continues (`READY ↔ DEGRADED`, `→ STOPPED/ERROR`).

### 2.6 The security reality

vLLM’s `--api-key`/`VLLM_API_KEY` authenticates **only** endpoints under the `/v1` prefix (and similar `/v2`, `/inference`). `/health` is unauthenticated; `/invocations` routes to the same inference functions as `/v1` but **bypasses** auth. So a key does **not** secure a network-reachable server. The TUI must reflect this and never imply otherwise (§7.9).

### 2.7 vLLM version drift is a first-class concern

Flag spellings, defaults, log strings, and enum value-sets change between releases (e.g. `gpu_memory_utilization` default `0.9` → `0.92`; request logging flipped from on-by-default `--disable-log-requests` to off-by-default `--enable-log-requests`; `--kv-cache-dtype` grew from 4 to ~15 values). This is handled by the `VllmProfile` abstraction (§7.2) and the rule **never bake vLLM defaults into our schema**.

-----

## 3. Scope & personas

**In scope (v1):** discover/validate named configs; preview the resolved command; launch (attached); stream colorized logs with phase detection; readiness via `/health`; stop/force-kill/restart; live GPU panel; search/filter/pause logs; durable scrubbed run log; single managed server at a time. **Out of scope (v1):** config-editing GUI; chat playground; multi-server tabs; multi-node; full MIG-slice attribution; auth for the TUI itself; detached mode is built but deferred to polished-v1 (§15).

**Personas:** the **researcher** (bring a known model up fast), the **lab admin** (reproducible configs; “leave it running”), the **debugger** (a failing load named clearly so they can fix the config).

-----

## 4. Functional requirements

**Config** — FR-1 discover valid configs in a configs dir (default `./configs`, overridable); FR-2 validate against a typed schema, showing field-level errors for invalid configs without crashing; FR-3 render the exact resolved command + env (secrets masked) before launch; FR-4 launch from a config named on the CLI (`vllm-loader run <name>`).

**Launch & lifecycle** — FR-5 launch vLLM as a child via the resolved command/env/cwd; FR-6 stop (SIGINT→SIGTERM→SIGKILL to the process group), force-kill (immediate SIGKILL), restart (stop then start same config); FR-7 optional **detached** launch surviving TUI exit, with reattach; FR-8 on quit while a server runs in **attached** mode, prompt Stop / Cancel (no runtime detach).

**Logs** — FR-9 capture child output, classify severity, display in a unified auto-scrolling colorized view; FR-10 render `\r` progress as live in-place updates, not blocking; FR-11 persist a **scrubbed, newline-committed** run log (`0600`); FR-12 filter by severity + free-text search/highlight; FR-13 pause/resume autoscroll, toggle wrap.

**Phase & readiness** — FR-14 maintain an authoritative phase from logs + health + process state; FR-15 phase timeline with per-phase + overall elapsed; FR-16 probe `/health` (unauth) → READY, then `/v1/models` (Bearer if keyed) for model name(s), showing the bind URL; FR-17 enforce a readiness timeout → `TIMED_OUT` with guidance; FR-18 keep polling after READY → `DEGRADED`/recover.

**GPU** — FR-19 per-GPU name/identity, mem used/total, util %, temp, power on an interval (NVML, `nvidia-smi` fallback); FR-20 degrade gracefully when unavailable.

**Errors** — FR-21 classify/surface OOM, port-in-use, model-not-found, TP-mismatch, HF-auth (gated model), generic non-zero exit, readiness timeout as named banners with the relevant excerpt.

**UX** — FR-22 every action reachable via `Ctrl+P`; FR-23 footer shows current bindings; FR-24 responsive layout (collapse sidebar/GPU on narrow terminals); FR-25 in-app help.

**Security** — FR-26 pass `api_key`/`HF_TOKEN` via env, never echoed; FR-27 scrub secrets from displayed *and* persisted logs; FR-28 warn when binding beyond localhost.

## 5. Non-functional requirements

- **NFR-1 Responsiveness:** input latency <100 ms regardless of log volume; no engine activity blocks the loop.
- **NFR-2 Log throughput:** sustain bursty init output via batched UI writes + a bounded ring buffer (~50k lines); persist everything (scrubbed) to disk.
- **NFR-3 Footprint:** zero GPU memory in the TUI; safe on a shared login node / over SSH.
- **NFR-4 Portability:** Linux primary; Python 3.10+ (3.11/3.12 rec.); usable down to 16-color/monochrome.
- **NFR-5 Reliability:** a child crash/hang never crashes the TUI; child exit is always detected and reported. No path lets a stalled logger block the child (§7.5).
- **NFR-6 Reproducibility:** the resolved command is deterministic and inspectable (FR-3); vLLM-owned defaults are not baked in (§7.3).
- **NFR-7 Self-observability:** a separate structured debug log behind `--debug` + Textual devtools.
- **NFR-8 Security:** secrets masked in previews and scrubbed from logs; never claim a server is secure because a key is set.
- **NFR-9 Maintainability:** all vLLM-version knowledge in the `engine` package / `VllmProfile`.
- **NFR-10 Testability:** engine/config/monitoring importable and testable without a terminal or GPU (fakes + recorded fixtures).

-----

## 6. Architecture

### 6.1 Components

```
                         ┌──────────────────────── TUI (Textual App) ───────────────────────┐
                         │ Header • Sidebar(ConfigList, PhaseTimeline, GpuPanel) •            │
                         │ LogView(RichLog) • ProgressLine • StatusStrip • Footer • Screens   │
                         └───────────────▲───────────────────────────▲─────────────────────────┘
                                         │  Textual Messages (events)  │
   ┌───────────────┬─────────────────────┼───────────────┬────────────┼────────────────────────┐
   │ Config         │ VllmProfile          │ Process/Launch │ Logging    │ Phase FSM   │ Monitoring│
   │ (schema/loader/│ (flags, defaults,    │ (PTY attached /│ sink       │ + parser    │ (GPU +    │
   │  registry)     │  enums, patterns,    │  supervisor    │ (decode/   │ (regex packs│  Health)  │
   │                │  soft validation)    │  detached)     │  split/    │  from       │  NVML /   │
   │                │                      │                │  scrub/tee)│  profile)   │  httpx    │
   └──────┬─────────┴───────────┬──────────┴───────┬────────┴─────┬──────┴─────────────┴────┬──────┘
          │ resolved cmd/env     │ profile          │ stdio (PTY/pipes)         durable file  │ HTTP
          └────► Command Builder ─┴──────────────────► vLLM child process  ◄────────────────── /health, /v1/*
```

### 6.2 Concurrency model (the responsiveness guarantee)

One asyncio loop (Textual’s). Heavy/blocking work is off it: the **engine** is a separate process; **log readers** are workers that `await` reads and post messages; **GPU sampling** is a thread worker (`post_message` is thread-safe, so no `call_from_thread` for posting; other UI mutations from threads do need it); **health probing** is an async timer worker. Optional workers (GPU, health) run with `exit_on_error=False` (§7.8) so they cannot crash the app.

### 6.3 Message taxonomy (decoupling contract)

Engine/monitoring layers never touch widgets directly; they emit typed Textual messages: `LogLineCommitted`, `LogLineTransient`, `ProgressUpdated`, `PhaseChanged`, `ServerReady`, `HealthChanged`, `ProcessExited`, `EngineError(kind,…)`, `GpuStatsUpdated`, `GpuStatsUnavailable`.

-----

## 7. Detailed component design

### 7.1 Configuration subsystem

YAML, one file per model, validated with **Pydantic v2**. **vLLM-owned fields default to unset (`None`)** so the installed vLLM’s own (evolving) defaults win when omitted; only fields the app owns get real defaults. Fast-evolving enums are open `str` (validated softly through the profile — §7.2); only genuinely stable enums are typed.

```yaml
# configs/llama-3.1-70b-awq.yaml
name: llama-3.1-70b-awq
description: Llama 3.1 70B, AWQ 4-bit, TP=4, 8k context
model: /models/Meta-Llama-3.1-70B-Instruct-AWQ      # local path OR HF repo id
served_model_name: llama-3.1-70b

command:
  entrypoint: serve           # serve | module
  executable: vllm            # optional override for the selected entrypoint

engine:                       # all vLLM-owned → unset unless set here
  tensor_parallel_size: 4
  gpu_memory_utilization: 0.92
  max_model_len: 8192
  dtype: auto                 # typed: auto|half|float16|bfloat16|float|float32
  quantization: awq           # open str (soft-validated)
  kv_cache_dtype: auto        # open str (soft-validated; ~15 valid values, version-dependent)
  enforce_eager: false
  # tensor/pipeline ints, swap_space, block_size, seed, max_num_seqs, … all optional

server:
  host: 127.0.0.1             # default localhost
  port: 8000
  exposure: local             # local | lan | public  (non-local requires explicit value + warning)
  api_key: null               # via env, masked, scrubbed from logs

logging:
  request_logging: false      # APP POLICY (opt-in). Enforced version-awarely (§7.3)
  suppress_access_log_for: [/health, /metrics]   # profile-gated (§7.3)
  max_log_len: null           # cap logged prompt chars (secret mitigation)

env:                          # extra child env (secrets excluded from sidecar)
  CUDA_VISIBLE_DEVICES: "0,1,2,3"
  HF_HOME: /data/hf-cache

extra_args: []                  # experimental/profile-unmodeled flags only; appended verbatim
                                # do not use for request logging; use logging.request_logging

vllm:
  version_profile: "0.11"     # optional; selects/validates a profile
  require_flags: []           # optional; fail early if installed binary lacks these

launch:
  mode: attached              # attached | detached
  ready_timeout_seconds: 900
  health: { path: /health, interval_seconds: 2 }
```

The **loader/registry** globs configs, validates each, keeps successes and failures separately (the picker shows valid configs and flags invalid ones with the Pydantic error), detects duplicate names, and resolves `served_model_name` from the model basename when unset.

### 7.2 `VllmProfile` (version adapter)

Isolates all version-specific knowledge so upgrades touch one place (NFR-9):

```python
@dataclass(frozen=True)
class VllmDefaults:
    request_logging: bool | None        # None = unknown for this version
    # … other policy-relevant defaults as needed …

@dataclass(frozen=True)
class VllmProfile:
    version: str                                  # detected, e.g. "0.11.2"
    flag_map: dict[str, str]                       # logical key -> CLI flag spelling
    defaults: VllmDefaults                         # what the installed version defaults to
    known_kv_cache_dtypes: frozenset[str]
    known_quantizations: frozenset[str]
    known_load_formats: frozenset[str]
    phase_rules: list[tuple[re.Pattern, "Phase"]]
    error_rules: list[tuple[re.Pattern, "ErrorKind"]]
    progress_re: re.Pattern
    known_flags: frozenset[str]                    # parsed from cached `vllm serve --help`

    def flag_for(self, key: str) -> str | None: ...
```

Startup detects the vLLM version, selects the closest bundled profile, caches `vllm serve --help`, and **soft-validates** configured flags/enum values against `known_*` → non-fatal warnings (parsing `--help` is itself version-fragile, and `extra_args` may use new flags). A config’s `vllm.require_flags` is the one place an explicit **hard** pre-launch gate exists.

### 7.3 Command builder

Pure function `ModelConfig → (argv, env, cwd)` (powers FR-3 preview). `command.entrypoint: serve` → `["vllm","serve",model,*flags]`; fallback `command.entrypoint: module` → `[python,"-m","vllm.entrypoints.openai.api_server","--model",model,*flags]`. `command.executable`, when set, overrides the binary for the selected entrypoint.

**The unifying flag-emission rule:** *emit a flag only when the desired behavior differs from the installed version’s default.* This resolves what looks like two policies into one:

- **Pass-through value fields** (`gpu_memory_utilization`, `max_model_len`, `dtype`, `kv_cache_dtype`, …): the desired behavior *is* “whatever vLLM defaults to,” so **unset → emit nothing**; emit the flag only if the user set the value. (Never bake vLLM’s defaults — they drift.)
- **App-policy knobs** (request logging; targeted access-log suppression): the app holds a stance, so it emits a flag **iff the stance differs from the profile’s default for that version** — and if the default is **unknown**, it emits the explicit flag to stay deterministic.

```python
# request-logging emission (version-aware; the canonical example of the rule)
def request_logging_flags(cfg, profile) -> list[str]:
    desired = cfg.logging.request_logging            # app policy, default False
    default = profile.defaults.request_logging        # True | False | None(unknown)
    if desired == default:
        return []                                      # behavior already matches → emit nothing
    key = "enable_request_logging" if desired else "disable_request_logging"
    flag = profile.flag_for(key)   # "--enable-log-requests" | "--no-enable-log-requests" | "--disable-log-requests" | None
    return [flag] if flag else []
# current vLLM (default off, desired off) → []   ;  older vLLM (default on, desired off) → ["--disable-log-requests"]
# user opt-in (desired on) → ["--enable-log-requests"]  ;  unknown default → explicit flag
```

Other builder rules: targeted access-log suppression emitted as `--disable-access-log-for-endpoints <paths>` only if `profile.flag_for("disable_access_log_for_endpoints")` exists; `--max-log-len N` if set; `served_model_name`, `host`, `port`, and set engine fields mapped via `profile.flag_map`; `extra_args` appended verbatim.

**Model-reference rule (local path vs repo id):** treat `model` as a **local path** only if it starts with `/`, `./`, `../`, `~`, or resolves to an existing relative path; otherwise treat it as a **repo id** and let vLLM/HF resolve it (so `org/model` is not misclassified as a missing path). Pre-flight existence checks run only for local paths.

**Env:** always `PYTHONUNBUFFERED=1` plus the config’s `env`; `api_key` exported as `VLLM_API_KEY` and **masked** (`••••`) in the preview; `HF_TOKEN` likewise.

### 7.4 Process & launch model

Two explicit modes; **the durable scrubbed file is the source of truth in both** (§7.5).

- **Attached (default; MVP):** spawn the child under a **PTY** (so tqdm/Rich emit live `\r` bars), in its own process group (`start_new_session=True`). The TUI reads the PTY master through the scrubbing sink. Quitting prompts **Stop / Cancel** only — **runtime detach is not offered**, because closing the PTY master on exit would `SIGHUP`/EOF the child. Survivability requires launching detached.
- **Detached (built; deferred to polished-v1):** a small **supervisor** process (double-fork + `setsid`) owns the child’s pipes, runs the scrubbing sink, and writes the durable file; the TUI merely **tails** that file and writes a sidecar (§7.10). “Detach” = the TUI stops tailing; supervisor + child keep running. Reattach reads the sidecar/manifest and resumes tailing + health-probing.

**Lifecycle:** stop = SIGINT → (timeout) SIGTERM → (timeout) SIGKILL to the **group**; force-kill = immediate SIGKILL; restart = stop then start same config. An awaited `proc.wait()` always emits `ProcessExited(code, signaled)`; exit before READY routes to the error classifier (§7.6 / FR-21).

> A “best of both” (live bars *and* survivability) via a **PTY-owning** supervisor is possible but deferred; the file-as-source-of-truth interface is forward-compatible with it.

### 7.5 Logging pipeline & the scrubbing sink

**Invariant:** raw child output never reaches the durable file or the UI directly — a **scrubbing log sink** always sits in between.

```
ATTACHED                                   DETACHED
child under PTY                            child (setsid) → OS pipes
   │ raw bytes                                │ raw bytes
PTY master read by TUI                     read by supervisor process
   └─► [ scrubbing log sink ] ◄── same code ──► [ scrubbing log sink ]
          ├─► scrubbed committed lines → durable file (0600, rotated; manifest §7.10)
          └─► scrubbed records → UI: committed → RichLog (as Text); transient \r → ProgressLine (NOT persisted)
```

The sink: reads raw bytes → **incremental UTF-8 decoder** (`codecs.getincrementaldecoder("utf-8", errors="replace")`) → **decode-then-split** on `\r`/`\n` (so multi-byte codepoints straddling chunk boundaries aren’t mangled) → scrub each segment → route. The durable file receives **scrubbed, newline-committed** lines only.

**Secret scrubbing (FR-27):** mask the configured `api_key`/`HF_TOKEN` plus generic patterns (`Authorization: Bearer \S+`, `sk-\S+`) to `Bearer ••••`, before both display and persistence. Defense in depth alongside §7.9’s request-logging-off default and `--max-log-len`.

**Bounded partial-line buffer (NFR-5):** a pathological unterminated line must not grow memory or stall the drain. Cap an unterminated segment at, e.g., **1 MiB**; on overflow, flush a scrubbed, truncated synthetic committed record (`[…line truncated at 1 MiB…]`), reset the pending buffer, and keep draining.

**PTY implementation notes (attached):**

- **EOF presents as `OSError`/`EIO` on Linux** when reading the PTY master after the slave closes — catch `errno.EIO` and treat as end-of-stream.
- **Close the slave fd in the parent immediately** after spawn, or the master never sees EOF on child exit (the reader hangs).
- **Set a fixed wide PTY window size** (`termios.TIOCSWINSZ`, e.g. 200 cols) and/or forward `SIGWINCH`, so progress lines aren’t wrapped/truncated at an assumed 80 cols (we parse, not re-render, the TTY).
- **Merged PTY loses stdout/stderr separation** (the MVP default; fine — vLLM logs mostly to stderr). Source tagging requires the optional two-PTY path or a prefixing wrapper.

**Supervisor robustness (detached):** the supervisor must **keep draining the child’s pipes even if file writes fail** (full disk, I/O error) — otherwise pipe buffers fill and the child blocks (the broken-pipe hazard moved one level up). It must outlive the TUI (separate session), be reaped sanely (its pid in the sidecar), and bound/rotate the durable file.

**Backpressure (NFR-2):** the LogView keeps a bounded ring buffer and batches writes; the durable file always receives every scrubbed committed line.

**vLLM log format (for the parser):** vLLM’s default formatter is `%(levelname)s %(asctime)s %(filename)s:%(lineno)d] %(message)s`, confirming the level-token-first shape the regexes rely on.

### 7.6 Phase FSM & log parser

Phases (§2.1) plus terminal `STOPPED`/`ERROR`. Error kinds: `OOM`, `PORT_IN_USE`, `MODEL_NOT_FOUND`, `TP_MISMATCH`, `HF_AUTH`, `CRASHED`, `TIMED_OUT`. The FSM consumes committed lines (hint-level), `ProgressUpdated`, `ServerReady`, and `ProcessExited`; loading phases are monotonic; post-READY allows `READY↔DEGRADED` and `→STOPPED/ERROR`. **Pattern packs live in the active `VllmProfile`.** Illustrative patterns (confirm per version):

|Signal             |Example substring                                               |Regex                                                             |
|-------------------|----------------------------------------------------------------|------------------------------------------------------------------|
|`STARTING`         |`Initializing a V1 LLM engine`; `world_size=`                   |`r"Initializing a .*LLM engine|world_size="`                      |
|`RESOLVING_MODEL`  |`Fetching N files`; `snapshot_download`                         |`r"Fetching \d+ files|snapshot_download|[Rr]esolv(e|ing) .*model"`|
|`DOWNLOADING_MODEL`|`Downloading …`; hf_transfer                                    |`r"[Dd]ownloading|hf_transfer"`                                   |
|`LOADING_WEIGHTS`  |`Starting to load model …`                                      |`r"Starting to load model"`                                       |
|weight progress    |`Loading safetensors checkpoint shards:  50% … 1/2`             |`r"checkpoint shards:\s+(\d+)%.*?(\d+)/(\d+)"`                    |
|`PROFILING_KV`     |`GPU KV cache size: … tokens`; `# GPU blocks:`                  |`r"GPU KV cache size|# GPU blocks|Maximum concurrency"`           |
|`CAPTURING_GRAPHS` |`Capturing CUDA graph shapes`                                   |`r"Capturing (?:CUDA )?graph"`                                    |
|server bound       |`Uvicorn running on http://…`                                   |`r"Uvicorn running on (https?://\S+)"`                            |
|`OOM`              |`CUDA out of memory`; `OutOfMemoryError`                        |`r"CUDA out of memory|OutOfMemoryError"`                          |
|`PORT_IN_USE`      |`address already in use`; `[Errno 98]`                          |`r"address already in use|Errno 98"`                              |
|`HF_AUTH`          |`GatedRepoError`; `401 Client Error`; `Cannot access gated repo`|`r"GatedRepoError|Cannot access gated repo|401 Client Error"`     |

`READY` is flipped by the **health probe** (logs say the server started; the probe proves it’s healthy — §7.7), not by a log line.

### 7.7 Health / readiness / liveness probing

Async `httpx` on a timer. **`/health` is probed unauthenticated** (it’s outside `/v1`); connection-refused/non-200 during load is “not ready yet,” not an error (gentle backoff). On first 200 → `ServerReady` (flips `READY`) and then **`GET /v1/models` with `Authorization: Bearer <api_key>` iff a key is configured** (a 401 there with a key set surfaces a specific “token mismatch?” hint, not a generic failure) to read served model id(s) + the bind URL. Enforce `ready_timeout_seconds` → `TIMED_OUT` distinguishing “still loading” vs “bound but unhealthy.” After READY, keep polling → `DEGRADED` on 200→non-200 while alive, recover on return. If `host` is non-loopback, probe `127.0.0.1`.

### 7.8 GPU monitoring

Primary **NVML** (`pynvml`): per-device name + **identity (UUID, MIG instance id when present)**, mem used/total, util %, temp, power; sampled every `gpu_interval_ms` in a **thread worker** (`exit_on_error=False`; `post_message` directly — thread-safe). Fallback **`nvidia-smi --query-gpu=…`** parsed into the same `GpuSample`. **CUDA visibility:** parse numeric and UUID forms of `CUDA_VISIBLE_DEVICES`; **display both the visible index and the NVML identity**; when mapping is ambiguous (MIG, opaque container/Slurm remap) **fall back to all NVML-visible GPUs** with a “mapping ambiguous” note. No promise of perfect MIG-slice attribution in v1. Never raise into the UI loop (FR-20).

### 7.9 Security

- **api-key scope:** `--api-key`/`VLLM_API_KEY` covers only `/v1` (and `/v2`,`/inference`); `/health` is unauthenticated; **`/invocations` bypasses auth** (routes to inference). The TUI never claims a server is secure because a key is set.
- **Probe behavior:** §7.7 (`/health` no auth; `/v1/models` Bearer).
- **Network exposure (FR-28):** default `server.host: 127.0.0.1`; binding non-loopback requires `exposure: lan|public` and shows a preview warning: *“Binds vLLM to {host}, reachable beyond localhost. `--api-key` does not protect all endpoints (e.g. `/invocations`). Put it behind a reverse proxy/firewall.”* Warning + explicit opt-in, not prohibition.
- **Secrets:** masked in previews (FR-26), scrubbed from displayed and persisted logs (§7.5/FR-27); request logging off by default and `--max-log-len` available (§7.3) so secrets rarely appear; never written to the sidecar (§7.10).

### 7.10 Sidecar, run manifest, reattach & pre-signal re-verification

Detached/reattach requires robust process identity (PID reuse is real, even mid-session).

**Sidecar** `runs/<run_id>.json` — identity only (no secrets):

```json
{
  "schema_version": 1, "run_id": "01J…ULID",
  "config_name": "llama-3.1-70b-awq", "config_snapshot": { "...": "resolved ModelConfig" },
  "command_argv": ["vllm","serve","..."], "command_hash": "sha256:…",
  "vllm_version": "0.11.2", "vllm_version_profile": "0.11",
  "executable": "/opt/venv/bin/vllm", "cwd": "/srv/models",
  "pid": 12973, "pgid": 12973,
  "process_create_time": 1780000000.123, "procfs_starttime": 8675309,
  "supervisor_pid": 12970, "supervisor_create_time": 1780000000.001,
  "supervisor_procfs_starttime": 8675280, "supervisor_executable": "/opt/venv/bin/python",
  "host": "127.0.0.1", "port": 8001, "served_model_names": ["llama-3.1-70b"],
  "exposure": "local", "launch_mode": "detached",
  "log_redaction": "scrubbed", "manifest_path": "runs/01J…ULID.manifest.json"
}
```

**Run manifest** `runs/<run_id>.manifest.json` — decouples stable identity from volatile log location (so rotation doesn’t fight the inode check):

```json
{ "active_log": { "path": "…/run.log", "inode": 12345678 },
  "rotated": [ { "path": "…/run.log.1", "inode": 12345677, "rotated_at": "…" } ] }
```

The supervisor **atomically updates the manifest on rotation** (write temp + rename).

**Reattach** verifies, before assuming control: (1) child PID alive **and** `process_create_time` matches (anti-PID-reuse); (2) `executable`+`cmdline` (or `command_hash`) match; (3) `pgid` matches; (4) in detached mode, the **supervisor** identity matches (its PID + create_time); (5) the **active** log inode from the manifest matches.

**Re-verify before every destructive signal:** identity (PID + `process_create_time`, and supervisor identity if detached) is re-checked **immediately before each stop/kill**, not only at reattach — a mismatch aborts with “tracked process is gone; refusing to signal a possibly-recycled PID.” This is the property that actually prevents signalling a recycled PID; the sidecar fields enable it, the per-action re-check enforces it.

-----

## 8. UI / UX design

### 8.1 Layout (wide terminal)

```
┌ vLLM Loader ─────────────────  llama-3.1-70b  ●READY  http://127.0.0.1:8000  12:42:07 ┐
│┌ Configs ───────────┐┌ Logs ──────────────────────────────────────────────────────┐│
││▸ llama-3.1-70b-awq ││ INFO  12:41:18 Loading weights took 14.30 seconds            ││
││  mistral-7b-fp8    ││ INFO  12:41:25 GPU KV cache size: 372,160 tokens             ││
││  ⚠ broken-cfg.yaml ││ INFO  12:41:31 Capturing CUDA graph shapes                   ││
│├ Phases ────────────┤│ INFO  12:41:48 Uvicorn running on http://127.0.0.1:8000      ││
││ ✔ Start  ✔ Weights ││ INFO  12:41:50 GET /health → 200                             ││
││ ✔ KV  ✔ Graphs     │└──────────────────────────────────────────────────────────┘│
││ ● READY (1m02s)    │ ▓▓▓▓▓▓▓▓░░ Capturing CUDA graphs 78%   ← ProgressLine (transient)│
│├ GPUs ──────────────┤                                                                │
││ 0 A100[UUID…] 38/80GB 64% ││ 1 A100 37/80GB 61% ││ 2 … ││ 3 …                       │
│└────────────────────┘ elapsed 1m02s · phase READY · 18,402 lines · autoscroll ON     │
├──────────────────────────────────────────────────────────────────────────────────┤
│ l Load  s Stop  K Kill  r Restart  / Search  f Filter  p Pause  ? Help  ^P Palette  q Quit │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 8.2 Screens

DashboardScreen (default); ConfigPickerScreen (modal; fuzzy list + resolved-command preview with masked env); ConfirmScreen (modal; kill, and quit-while-attached → Stop/Cancel); HelpScreen (modal; bindings + palette hint).

### 8.3 Widgets

`StatusBadge` (reactive `phase`; pulses while loading), `PhaseTimeline` (✔/●/○ + elapsed), `GpuPanel` (per-GPU rows incl. identity), `ConfigList` (valid + ⚠ invalid), `LogView` (`RichLog`), `ProgressLine` (`ProgressBar` driven by `ProgressUpdated`), `ErrorBanner`.

### 8.4 LogView rendering (corrected)

`RichLog(markup=False, highlight=False)`. Only **committed** lines are written, and **as `rich.text.Text`** with an explicit per-level style — never via the markup parser (raw logs contain `[brackets]`/ANSI → `MarkupError`/mis-styling, and it’s an injection vector). **Transient** `\r` records drive `ProgressLine` and are **never** written to `RichLog`.

```python
LEVEL_STYLE = {"CRITICAL":"bold white on red","ERROR":"bold red","WARNING":"yellow","INFO":"","DEBUG":"dim"}

@on(LogLineCommitted)
def _on_line(self, m):
    self.fsm.feed(m.text)                                  # committed = phase hint
    self.query_one("#log", RichLog).write(Text(m.text, style=LEVEL_STYLE.get(m.level, "")))

@on(LogLineTransient)
def _on_progress(self, m):
    self.query_one(ProgressLine).update_from(m)            # progress widget only; not persisted
```

### 8.5 Keybindings & command palette

`l`/`Enter` load · `s` stop · `K` kill · `r` restart · `c` picker · `/` search · `f` filter · `p` pause · `w` wrap · `g/G` top/bottom · `Tab` focus · `?`/`F1` help · `Ctrl+P` palette · `q`/`Ctrl+C` quit (confirm if running). Every action is also a palette command (e.g. “Load config: …”, “Restart server”, “Copy server URL”, “Reattach to running server”).

### 8.6 Theming & responsiveness

TCSS dark theme, one accent, generous spacing. Status colors paired with icons/words (monochrome-usable): IDLE grey, loading amber (pulse), READY green, DEGRADED amber, ERROR red, STOPPED grey. `HORIZONTAL_BREAKPOINTS`: <100 cols collapse sidebar to overlay; <60 cols drop GPU panel; the log never disappears. Toasts for state changes; `ErrorBanner` for `EngineError` with cause + suggestion + jump-to-lines.

-----

## 9. Technology stack

Python 3.10+ (3.11/3.12 rec.); **textual** (+ **rich** transitively); **pydantic** v2; **PyYAML**; **httpx** (async probes); **nvidia-ml-py** (`pynvml`); **psutil** (process identity incl. `create_time()`, fallbacks); **typer** (CLI: `run`/`list`/`preview`/`version`); optional `tomllib`, `watchfiles`. `vllm` is the **child target**, not imported by the TUI (so it installs on non-CUDA machines). Tooling: `pyproject.toml` console-script `vllm-loader`; uv/pip; ruff; pytest + pytest-asyncio. Pin exact versions after testing against the lab’s vLLM build.

## 10. Project structure

```
vllm-loader/
├── pyproject.toml · README.md
├── configs/                       # example configs (127.0.0.1, exposure, version_profile)
├── src/vllm_loader/
│   ├── cli.py · messages.py
│   ├── config/ { schema.py, loader.py, registry.py }
│   ├── engine/ { profile.py, command_builder.py, process_manager.py,
│   │             supervisor.py, log_sink.py, phases.py, errors.py, sidecar.py }
│   ├── monitoring/ { gpu.py, health.py }
│   └── tui/ { app.py, styles.tcss,
│              screens/{dashboard,config_picker,confirm,help}.py,
│              widgets/{status_badge,phase_timeline,gpu_panel,config_list,log_view,progress_line,error_banner}.py }
└── tests/ { fixtures/*.log+*.json, test_command_builder.py, test_request_logging_policy.py,
            test_phases.py, test_log_sink.py, test_config_loader.py, test_sidecar.py }
```

## 11. Key code skeletons

Command-builder request-logging (§7.3) and LogView rendering (§8.4) are above. Worker hardening:

```python
from textual import work, on
from textual.worker import Worker, WorkerState

@work(group="gpu", thread=True, exit_on_error=False)         # cannot crash the app
def gpu_monitor(self):
    try: nvml = open_nvml()
    except Exception as e: self.post_message(GpuStatsUnavailable(str(e))); return   # post_message thread-safe
    while True:
        try: self.post_message(GpuStatsUpdated(sample_once(nvml)))
        except Exception as e: self.post_message(GpuStatsUnavailable(str(e))); return
        time.sleep(self.settings.gpu_interval_ms / 1000)

@work(group="health", exit_on_error=False)
async def health_probe(self, cfg):
    try: await probe_loop(cfg, self.post_message)             # /health unauth, /v1/models Bearer
    except Exception as e: self.post_message(HealthChanged(status="unknown", detail=str(e)))

def on_worker_state_changed(self, ev: Worker.StateChanged):   # backstop
    if ev.state is WorkerState.ERROR and ev.worker.group in {"gpu","health"}:
        self.notify(f"{ev.worker.group} monitor stopped: {ev.worker.error}", severity="warning")
```

Scrubbing sink with bounded buffer (essence):

```python
MAX_UNTERMINATED = 1 << 20
async def run_sink(reader, post, file, scrub):
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    while True:
        try: chunk = await reader.read(4096)
        except OSError as e:
            if e.errno == errno.EIO: break                    # PTY EOF on Linux
            raise
        if not chunk: break
        pending += dec.decode(chunk)
        i = 0
        for m in re.finditer(r"[\r\n]", pending):
            seg, term = pending[i:m.start()], pending[m.start()]
            line = scrub(seg)
            if term == "\n":
                file.write((line + "\n").encode()); post(LogLineCommitted(line))
            else:
                post(LogLineTransient(line))                  # not persisted
            i = m.end()
        pending = pending[i:]
        if len(pending) > MAX_UNTERMINATED:                   # don't grow / don't stall the drain
            line = scrub(pending[:MAX_UNTERMINATED]) + " […truncated…]"
            file.write((line + "\n").encode()); post(LogLineCommitted(line)); pending = ""
```

## 12. Error handling & edge cases (selected)

Invalid config → ⚠ in picker with the Pydantic error. `vllm` not on PATH → “install vLLM or set `command.entrypoint: module`.” Model path missing (local only) → `MODEL_NOT_FOUND`. Gated/private HF model → `HF_AUTH` (“set HF_TOKEN / accept the license”). OOM → suggest lower `gpu_memory_utilization`/`max_model_len`. Port in use → name the port. TP×PP > GPUs → explain world-size. Child exit before READY → `CRASHED` + last error lines. Bound but unhealthy → `TIMED_OUT` distinguishing cause. Log firehose → bounded buffer + batched writes; full scrubbed log to disk. Unterminated giant line → truncated synthetic record (§7.5). NVML/`nvidia-smi` absent → “GPU stats unavailable” placeholder. Quit while attached → Stop/Cancel. Terminal too small → collapse panels. `api_key` set → env + masked + scrubbed. PID recycled before a signal → abort the signal (§7.10).

## 13. Testing strategy

Unit (no terminal/GPU): `test_command_builder` (exact argv/env; masked secrets; boolean elision); `test_request_logging_policy` (matrix over profiles: current-default-off, older-default-on, user-opt-in, unknown-default — assert correct/empty flag emission); `test_phases` (recorded fixtures: success, OOM, port-in-use, **HF download/cache-miss**, **gated-model**); `test_log_sink` (`\r`/`\n` across chunk boundaries; multi-byte split via incremental decode; **bounded-buffer truncation**; **secret scrubbing in displayed *and* persisted output**; PTY EIO path); `test_sidecar` (identity verify incl. supervisor; manifest rotation update; reject recycled PID); `test_config_loader`. Component: fake child (emits canned vLLM output incl. `\r` bars through a PTY). TUI smoke: `App.run_test()`/`Pilot`. Manual: bring up a real 7B; verify full phase walk, READY URL, stop/restart, detach/reattach, GPU panel, degraded recovery.

## 14. Packaging, distribution & ops

`pip install .` → `vllm-loader` (+ `run`/`list`/`preview`/`--version`). Config discovery: `--configs-dir` › `VLLM_LOADER_CONFIGS` › `./configs` › `~/.config/vllm-loader/configs`. Run artifacts (logs `0600`, sidecars, manifests) under `~/.local/state/vllm-loader/runs/`. Remote: run over SSH on the GPU host, or `textual serve` for browser access **gated behind your network/auth** (it controls model launches). Document the tested vLLM version range; add log fixtures + a `VllmProfile` on each bump.

## 15. Implementation plan (~1 week MVP / ~2–3 weeks polished v1)

Phases are independently demoable. Estimates assume one engineer fluent in async Python.

- **P0 Scaffolding & config (~0.5–1d):** pyproject, package skeleton, ruff/pytest, CLI stub; `config/schema.py` (unset-default pass-through; open enums), loader/registry; `vllm-loader list`/`preview`. *Done when:* lists valid configs, flags invalid ones, prints a correct resolved command.
- **P1 Profile + command builder (~1d):** `VllmProfile` (detect version, cache `--help`, defaults table, soft validation); builder incl. the version-aware flag-emission rule and model-ref rule. *Done when:* `test_command_builder` + `test_request_logging_policy` green.
- **P2 Process + PTY + scrubbing sink (~1.5–2d):** attached PTY launch (close-slave, fixed width, EIO), `log_sink.py` (incremental decode-then-split, scrub, bounded buffer, tee to `0600` file), stop/restart, exit detection. *Done when:* launches `vllm serve` (or fake child), streams scrubbed lines to UI+file, renders `\r` progress, stops gracefully; `test_log_sink` green.
- **P3 Phase FSM + errors (~1d):** `phases.py` (packs from profile), `errors.py`; capture real vLLM fixtures. *Done when:* `test_phases` reproduces the success walk + OOM/port/HF-auth classifications.
- **P4 Minimal Textual UI (~1–2d):** Header/Footer/`RichLog`(Text)/`ProgressLine`, load/stop bindings, message wiring. *Done when:* live colorized stream + transient progress + phase in header; no freezes under bursts.
- **P5 Sidebar + readiness + GPU (~1.5–2d):** ConfigList/PhaseTimeline/StatusBadge/status strip; health probe (`/health` unauth → READY; `/v1/models` Bearer; timeout; degraded polling); GPU panel (NVML+fallback, identity, `exit_on_error=False`). *Done when:* full visual phase walk; READY flips on real 200 with URL+model; degraded recovers; per-GPU stats.

**MVP = P0–P5** (attached-only): genuinely useful, ship-to-lab.

- **P6 Polish & discoverability (~1–2d):** command-palette commands, search/filter/pause/wrap, toasts, `ErrorBanner`, Help/Confirm screens, breakpoints, theme.
- **P7 Detached + robustness (~1.5–2d):** supervisor (drain-always, rotation), sidecar + manifest + reattach + pre-signal re-verify, force-kill, all §12 edge cases, signal-safe shutdown, `--debug` self-log.
- **P8 Tests/docs/packaging (~1d):** round out the suite, README + GIF, example configs, pin tested vLLM range, publish.

**Polished v1 = P0–P8 (~2–3 weeks).**

## 16. Future enhancements

In-app config editing; multiple servers/tabs; an inference playground tab (test prompt, tokens/sec); live metrics by scraping vLLM’s Prometheus `/metrics`; parameter presets (throughput vs latency); the PTY-owning supervisor (live bars *and* survivability); ROCm `rocm-smi` telemetry; full MIG-slice attribution; `systemd`/tmux unit generation; theme switcher + saved UI prefs.

-----

## Appendix A — Example config (Mistral 7B, FP8)

```yaml
name: mistral-7b-fp8
description: Mistral 7B Instruct, FP8, single GPU, 16k context
model: mistralai/Mistral-7B-Instruct-v0.3
served_model_name: mistral-7b
engine: { tensor_parallel_size: 1, gpu_memory_utilization: 0.90, max_model_len: 16384,
          dtype: auto, kv_cache_dtype: fp8, enforce_eager: false }
server: { host: 127.0.0.1, port: 8001, exposure: local }
logging: { request_logging: false, suppress_access_log_for: [/health] }
env: { CUDA_VISIBLE_DEVICES: "0" }
vllm: { version_profile: "0.11" }
launch: { mode: attached, ready_timeout_seconds: 600 }
```

## Appendix B — Verified facts & sources

- **vLLM load log lines** (`Starting to load model`, `Loading safetensors checkpoint shards: …%`, `Loading weights took …`, `GPU KV cache size`, `Capturing CUDA graph`, `Uvicorn running on …`, worker prefixes, default formatter `%(levelname)s %(asctime)s %(filename)s:%(lineno)d]`): vLLM issues #39010/#12308/#13765, “How to read vLLM logs” (v0.11.2), logging-configuration docs.
- **Engine args / defaults:** `gpu_memory_utilization` default `0.92` in current stable (serve CLI, `configuration/engine_args`, config API `Field(default=0.92)`) vs `0.9` in older (v0.10.2) + a stray `0.9` metrics example → *do not bake defaults*. `--dtype {auto,half,float16,bfloat16,float,float32}`. `--kv-cache-dtype` ~15 values in current (`auto,bfloat16,float16,fp8,fp8_ds_mla,fp8_e4m3,fp8_e5m2,fp8_inc,fp8_per_token_head,int8_per_token_head,nvfp4,turboquant_*`) vs 4 in older → *open str + soft validation*. (`docs.vllm.ai` stable/latest CLI & engine-args; older version pages.)
- **Request logging:** current `--enable-log-requests` (default off) / `--no-enable-log-requests`; older on-by-default + `--disable-log-requests` (vLLM #1240) → *version-aware emission*. Targeted controls `--disable-access-log-for-endpoints`, `--disable-uvicorn-access-log`, `--max-log-len` (`docs.vllm.ai/en/v0.11.0/cli/serve.html`, latest logging-configuration).
- **Security:** `--api-key`/`VLLM_API_KEY` covers only `/v1` (and `/v2`,`/inference`); `/v1/models` Bearer-protected; `/health` unauthenticated; `/invocations` bypasses auth; “do not rely exclusively on –api-key” (`docs.vllm.ai/en/stable/usage/security/`). Bearer tokens can be logged in plaintext (vLLM hardening guidance; production-stack #819) → *scrub + off-by-default request logging*.
- **Textual:** `RichLog.write()` appends (use a separate widget for transient `\r`); `markup=True` parses bracket markup (use `rich.text.Text`); worker `exit_on_error=True` default (set `False` for optional monitors); `post_message` thread-safe while other UI calls from threads need `call_from_thread`; `thread=True` required for thread workers; `HORIZONTAL_BREAKPOINTS`; command palette `Ctrl+P`; `textual serve` (`textual.textualize.io` widgets/rich_log, guide/workers, api/worker, api/app; Textualize GitHub).
- **PTY:** Linux PTY master read after slave close raises `OSError`/`EIO`; incremental UTF-8 decoding via `codecs.getincrementaldecoder` (POSIX pty semantics; `pty`/`pexpect`/`ptyprocess` practice).

## Appendix C — Glossary

TUI; PagedAttention; KV cache / blocks; tensor parallelism (TP); pipeline parallelism (PP); CUDA graph capture; `gpu_memory_utilization`; `served_model_name`; FSM; NVML; reactive/worker/TCSS (Textual); PTY (pseudo-terminal); scrubbing sink; sidecar/manifest; `VllmProfile`. (Definitions as in the body above.)
