# Vela — Docker Runtime (First-Class Container Deployments) — Feature Specification (v1)

**Feature:** make **Docker a first-class runtime** in Vela so it natively generates, launches, supervises, and stops vLLM containers — replacing hand-written wrapper scripts while following the lab's established docker convention. · **Status:** spec-ready · **Audience:** the engineer(s) extending `vela`.

> **Relationship to existing specs.** Additive extension to the canonical loader (process/launch model §7.4, sidecar/identity §7.10, logging §7.5, health §7.7, phase FSM §7.6) and the agent/controller spec (lifecycle authority §5.3). It introduces a **second launch backend** — `DockerRuntime` — alongside the existing process backend, selected by a new `command.runtime` field. The serve-args still come from the existing command builder (`engine/command_builder.py`); only the *spawn/monitor/stop* shell changes.

> **Why first-class, not wrappers.** Today the lab runs vLLM in Docker via hand-written foreground wrappers (`scripts/blackbird_qwen36_*_vllm_foreground.sh`) plugged in as `command.executable`. That works but is **per-deployment shell to maintain**, easy to get wrong (forgot `--ipc=host`, wrong KV pin for BF16 — see the FP8-vs-BF16 footgun), and invisible to Vela (Vela monitors the wrapper PID, not the container). Making Docker a runtime lets Vela **own** the run command (correct-by-default), the **container lifecycle** (stop = `docker stop`, not SIGINT-to-wrapper), and **container identity** (sidecar tracks the container, not a shell PID). The composer (`vela-deployment-composer-spec-v1.md`) can then generate docker deployments with **no wrapper script**.

---

## 0. Grounding: vLLM Docker best practices (verified, Appendix A)

The lab's wrappers already match vLLM's official guidance; Vela should **codify** it. Confirmed against the official vLLM "Using Docker" docs and 2026 production guides (Appendix A):
- Official image `vllm/vllm-openai` (Docker Hub); **pin by version tag or digest**, never `:latest`, in production.
- Canonical run: `docker run --runtime nvidia --gpus all -v <hf-cache>:/root/.cache/huggingface --env HF_TOKEN=… -p 8000:8000 --ipc=host vllm/vllm-openai:<tag> --model <model> [vllm serve flags]`.
- **`--ipc=host` (or `--shm-size`) is required** — PyTorch shared memory for tensor-parallel/NCCL; 16g single-GPU, 32g multi-GPU.
- **Mount the HF cache** so weights persist across restarts.
- **`--gpus all` / `--gpus '"device=0,1"'`**; `CUDA_VISIBLE_DEVICES` per container for isolation (empty → silent CPU fallback).
- Health: `/health` (liveness, ~15–30 s after spawn) + `/v1/models` (readiness, minutes for big models) → generous start-period.
- Lifecycle: name the container; `docker logs -f` to stream; **vLLM handles SIGTERM gracefully** (`docker stop -t <grace>`); restart policy `unless-stopped` *for self-managed* deployments — **but Vela owns lifecycle, so Vela uses `--restart no`** and supervises.

---

## 1. Vision & elemental concepts

### 1.1 A runtime is "how Vela spawns and supervises the server"
Vela already abstracts launch behind `engine/process_manager.py` (attached PTY / detached supervisor) and the agent's always-supervised model (agent §5.2). The **runtime** is the backend that turns a resolved launch spec into a running, monitorable, stoppable workload:
- **`process`** (today) — spawn `<executable> serve <model> <flags>` as a child process group; stop = signal the group; identity = PID + create_time + pgid.
- **`docker`** (new) — generate `docker run …` from config; the workload is a **container**; stop = `docker stop`; identity = container **name + id + image digest**.

### 1.2 The image IS the build
For docker deployments there is no managed venv. The vLLM "build" is the **image** (a pinned `vllm/vllm-openai` tag/digest or a lab-built image like `vllm-mxfp4-bw-sm120`). So `command.runtime: docker` is an **alternative to `command.build`/`command.executable`** in the precedence chain (§5).

### 1.3 Vela owns the container, Docker runs it
The agent generates and issues `docker run`, attaches to logs, probes health, and on stop/kill/restart re-verifies container identity then `docker stop/kill`. The container is `--restart no` and runs in the **agent's** lifecycle authority — exactly the canonical "verify-before-every-destructive-signal" discipline, but the "signal" is a Docker command and the "identity" is the container.

### 1.4 Correct-by-default, fully overridable
Vela injects the required flags (`--gpus`, `--ipc=host`/`--shm-size`, cache volume, `HF_TOKEN`, host/port) from best practice + the config, while leaving every docker knob and every vLLM serve flag overridable — same philosophy as the composer.

---

## 2. Scope & non-goals

**In scope (v1):** a `DockerRuntime` launch backend; the `command.runtime`/`command.docker` schema; `docker run` generation from config + defaults; container-identity sidecar + verify-before-signal; stop/kill/restart via docker; log streaming + scrubbing; health on the published/host port; phase FSM over container logs; sibling-eviction + port guard; standalone export; a fake-docker test harness. **Out of scope (v1):** Docker Compose / multi-container stacks (future); Kubernetes; remote Docker daemons (the agent runs `docker` locally on its host); image building from a Dockerfile (use `vela build` or a pre-built image); rootless-docker specifics beyond honoring the host's setup; non-NVIDIA device wiring beyond passthrough fields (ROCm fields provided, untested).

---

## 3. Functional & non-functional requirements

**Schema & generation**
- **FR-D1** New `command.runtime: process | docker` (default `process`; back-compat) and a `command.docker` block (§4).
- **FR-D2** Generate the full `docker run` argv from `command.docker` + best-practice defaults + the vLLM serve args produced by the existing command builder.
- **FR-D3** Defaults applied when unset: `--gpus all`, `--ipc=host` (or `--shm-size` if `ipc:false`, sized by `tensor_parallel_size`), HF-cache volume mount, `HF_TOKEN` env (gated), `--name <container_name>`, `--restart no`, the host/port mapping or `--network host`.
- **FR-D4** Image must be resolvable on the target (`docker image inspect`); **pin-by-digest recommended**, and the agent records the **resolved digest** in the sidecar.

**Lifecycle (agent-side authority)**
- **FR-D5** Launch: `docker run -d --name <c>` then attach `docker logs -f` + `docker wait`; READY is health-driven (canonical §7.7), not a log line.
- **FR-D6** Stop: re-verify container identity, then `docker stop -t <grace>` (SIGTERM, graceful); Kill: `docker kill`; Restart: stop + run with a new run id.
- **FR-D7** **Verify-before-every-destructive-action** (the docker analogue of §7.10): the container still exists, its **id matches** the sidecar's recorded id, and its image digest matches — else abort with `identity-verification-failed` (no acting on a recycled name).
- **FR-D8** Sibling eviction: optionally `docker stop` a configured list of conflicting container names before launch (the single-GPU "one big model" pattern); refuse if the target port is already bound.

**Observability & safety**
- **FR-D9** Stream container logs through the **scrubbing LogSink** (§7.5) → durable 0600 log + scrubbed events; never raw.
- **FR-D10** Health/readiness via the existing probe against the published/host port; `reachable_url` from the target host (agent §9.4).
- **FR-D11** Phase FSM consumes container log lines unchanged (same regex packs); `BuildErrorKind`/serve `ErrorKind` classification applies.
- **FR-D12** Secrets (`HF_TOKEN`, `api_key`) passed as `-e` from agent env, masked in preview, scrubbed in logs, redacted in any export.

**Surfaces**
- **FR-D13** `vela preview` renders the exact masked `docker run`; `vela run/smoke/smoke-tui` and the TUI lifecycle (`l/s/K/r`) work identically for docker deployments.
- **FR-D14** `vela deploy create --runtime docker` (composer) and `vela build adopt-image`? (optional: register a known-good image as a selectable "docker build").
- **FR-D15** Standalone export: emit a self-contained `docker run` script (secrets redacted) reproducing the launch (parity with build `run.sh`).

**Non-functional**
- **NFR-D1** Back-compat: existing wrapper-based (`command.executable`) configs are unchanged; docker runtime is additive and opt-in.
- **NFR-D2** Identity rigor: container id + image digest give anti-reuse equivalent to PID+create_time.
- **NFR-D3** Crown-jewel preserved: the controller never runs `docker`; all docker ops are agent-side.
- **NFR-D4** Testable without Docker/GPU: a fake `docker` executable (canned `run`/`logs`/`wait`/`inspect`/`stop`) drives the FSM and lifecycle in tests.
- **NFR-D5** No hidden host mutation: Vela only creates the configured container + named caches; `--restart no`; no `/var/run/docker.sock` mount.

---

## 4. Schema: `command.runtime` + `command.docker`

```yaml
command:
  entrypoint: serve            # unchanged; serve args still built by the command builder
  runtime: docker              # NEW: process | docker  (default process)
  docker:                      # NEW: only when runtime == docker
    image: vllm/vllm-openai@sha256:b13d6e5…   # tag or digest (digest recommended)
    container_name: vela-qwen36-27b-bf16        # default: vela-<config name>
    gpus: all                  # all | "device=0,1" | count:2
    ipc_host: true             # default true; if false, set shm_size
    shm_size: 32g              # default 16g (single) / 32g (TP>1) when ipc_host=false
    network: host              # host | bridge   (bridge → publish -p <port>:<port>)
    volumes:                   # HF cache auto-added if absent; extra mounts here
      - /home/bgconley/models/qwen36-27b-bf16:/home/bgconley/models/qwen36-27b-bf16
    env:                       # passthrough container env (merged with model HF env)
      HF_HOME: /home/bgconley/models/qwen36-27b-bf16/hf-cache
    hf_cache: /home/bgconley/models/qwen36-27b-bf16/hf-cache   # mounted to the in-image HF cache path
    restart: "no"              # vela owns lifecycle; default "no"
    stop_grace_seconds: 90     # docker stop -t
    entrypoint: null           # override container entrypoint if needed
    pull: never                # never | missing | always
    evict:                     # sibling container names to stop before launch
      - qwen36-27b-fp8-kvfp8-rp6000-vela
    extra_run_args: []         # raw passthrough docker run args (escape hatch)
    # ROCm/other passthrough (untested in v1): devices, cap_add, security_opt, group_add
```
The vLLM serve flags continue to come from `model`, `engine.*`, `server.*`, `extra_args` via the existing builder. `command.runtime: docker` and `command.executable`/`command.build` are **mutually exclusive** (validator).

---

## 5. Resolution & precedence (extends build precedence)
Which vLLM launches, highest→lowest:
1. `command.executable` — raw process override (unchanged).
2. **`command.runtime: docker` + `command.docker.image`** — the container is the runtime; **no build/executable**.
3. `command.build` — managed venv.
4. Global default build.
5. Bare `vllm` on PATH.
`docker` and `executable`/`build` are mutually exclusive. The model handoff (model spec §9: `model_arg`, `--revision`, tokenizer, HF env) folds into the container's `--model`/`-e` for docker exactly as it folds into argv/env for process.

---

## 6. The DockerRuntime backend (agent-side)

### 6.1 Generation (`build_docker_run`)
Pure function `(ModelConfig, resolved_serve_args, defaults) → docker_argv`:
```
docker run -d --name <container_name> --restart no
  [--runtime nvidia] --gpus <gpus>
  (--ipc=host | --shm-size <shm_size>)
  [--network host | -p <port>:<port>]
  -v <hf_cache>:<in-image hf cache>  [+ extra volumes]
  -e HF_TOKEN=<from agent env, scrubbed in preview>  [+ env, + model HF env]
  [--entrypoint <override>] [extra_run_args…]
  <image>
  <model> <serve flags…>                # from the command builder (positional model + flags)
```
Defaults injected only when unset (FR-D3). `--shm-size` defaults: `tensor_parallel_size>1 → 32g`, else `16g`. The in-image HF cache path defaults to `/root/.cache/huggingface` (or `/home/vllm/.cache/huggingface` for non-root images — detectable/overridable).

### 6.2 Launch & supervise
Mirrors the proven lab wrapper, but Vela-owned:
1. Resolve + record the image **digest** (`docker image inspect`); pull per `pull:` policy.
2. Evict siblings (FR-D8); port guard (`ss`/`docker ps`).
3. `docker run -d --name <c> …` → capture container id.
4. Write the **sidecar** (§7) with `{container_name, container_id, image_digest, port, host, served_model_names, runtime: "docker"}`.
5. Attach `docker logs -f <c>` → drain into the scrubbing LogSink → events; start health probe; feed the phase FSM.
6. `docker wait <c>` in the background → on exit emit `exited(returncode)`.
The supervisor process model is unchanged (the run survives the agent; the container is `--restart no` but persists independent of the agent, re-discoverable by name+id).

### 6.3 Stop / kill / restart (verify-before-act)
```
stop:    verify_container_identity(sidecar) → docker stop -t <grace> <id>
kill:    verify_container_identity(sidecar) → docker kill <id>
restart: stop → docker run (new run_id)
verify_container_identity: docker inspect <id> exists AND .Name == container_name AND .Image digest == sidecar.image_digest
                           else raise TrackedProcessMismatch → -32002 (agent §P1 contract)
```
This is the docker analogue of canonical §7.10 / agent §5.3 — re-verify immediately before every destructive action; a recycled container **name** with a different **id/digest** aborts the action.

### 6.4 Discovery / reattach
`discover_runs` includes docker runs (sidecar `runtime: docker`); reattach verifies the container id/digest still match and re-attaches `docker logs -f` from the durable-log offset (canonical §7.10 resume, applied to the container log tail).

### 6.5 Health, phase, errors
Unchanged: health probes `127.0.0.1:<port>` on the agent (container uses `--network host` or `-p`), `reachable_url` from the target host; the phase FSM reads container log lines; OOM/port/HF-auth/etc. classification applies. Docker-specific failures add `DockerErrorKind {IMAGE_NOT_FOUND, IMAGE_PULL_FAILED, DAEMON_UNREACHABLE, NAME_CONFLICT, OCI_RUNTIME_ERROR, GPU_NOT_AVAILABLE}` surfaced as named banners.

---

## 7. Sidecar & identity (docker variant)
Extend the sidecar (`engine/sidecar.py`) with a typed runtime discriminator and docker identity:
```json
{ "runtime": "docker",
  "container_name": "vela-qwen36-27b-bf16", "container_id": "9f3c…",
  "image": "vllm/vllm-openai@sha256:b13d6e5…", "image_digest": "sha256:b13d6e5…",
  "host": "0.0.0.0", "port": 18002, "served_model_names": ["qwen36-27b-bf16-rp6000"],
  "started_at": …, "supervisor_pid": … }
```
`verify_sidecar_identity` gains a docker path: container exists + id + digest match (instead of PID/create_time/pgid). No secrets in the sidecar.

---

## 8. Security (extends canonical §7.9)
- **Image pinning:** prefer digests; record the resolved digest; warn on `:latest`.
- **No daemon socket mount; `--restart no`; no privileged** by default; ROCm device/cap fields are explicit opt-in.
- **Secrets:** `HF_TOKEN`/`api_key` injected as `-e` from agent env, **masked in preview**, **scrubbed in logs** (LogSink), **redacted in exports**; never written to config/sidecar/manifest.
- **Exposure:** `--network host` on `0.0.0.0` is non-loopback → requires `exposure: lan|public` + the canonical warning (FR-D10).
- **Scrub-before-wire** unchanged: container logs pass the LogSink before any event/durable write.

---

## 9. Migration & coexistence
- **Existing wrapper configs keep working** (they're `command.executable`; untouched). The lab can migrate `qwen36-27b-*-blackbird` from the wrapper to `runtime: docker` deployment-by-deployment, and the composer will generate native docker configs going forward.
- **Standalone export (FR-D15)** gives the same "no Vela required" guarantee the wrappers had.
- A migration helper (`vela deploy from-wrapper <config>`) can read a wrapper-based config + its script and emit an equivalent native-docker config (best-effort, review-required).

---

## 10. Testing strategy (no Docker / no GPU)
A **fake `docker`** executable on PATH (like `fake_vllm_child.py`) that responds to `image inspect`/`run`/`logs`/`wait`/`inspect`/`stop`/`kill`/`ps` with canned output, plus recorded vLLM container-log fixtures:
- **`test_docker_run_generation`** — argv from config: defaults injected (`--ipc=host`/`--shm-size` by TP, cache volume, `HF_TOKEN`, name, `--restart no`, port/network), digest used, mutual-exclusion with `executable`/`build`, BF16 vs FP8 not over-pinning KV.
- **`test_docker_lifecycle`** — launch records sidecar (name/id/digest); stop → `docker stop -t`; kill → `docker kill`; restart; **verify-before-act** → recycled name/id aborts with `-32002`.
- **`test_docker_logs_scrub`** — container logs stream through LogSink; secrets masked in events + durable log.
- **`test_docker_health_phase`** — FSM walks STARTING→…→READY from fake container logs + health; `reachable_url` from target host.
- **`test_docker_errors`** — image-not-found / pull-failed / name-conflict / port-in-use / gpu-not-available named banners.
- **`test_docker_discover_reattach`** — discover/reattach verifies id+digest; resume by log offset.
- **`test_docker_export`** — standalone script reproduces the run with secrets redacted.
- **TUI smoke** — a docker deployment loads/stops via the normal `l/s/K/r` path.
**Manual:** the real Blackbird Qwen3.6 27B (FP8 and BF16) via `runtime: docker` reaching READY on `18003`/`18002`, stop = `docker stop`.

---

## 11. Implementation phases (each shippable)
- **DK0 Schema + generation (~1.5d):** `command.runtime`/`command.docker`, `build_docker_run` pure function, mutual-exclusion validator, `preview` renders the masked `docker run`. *Done when:* `test_docker_run_generation` green; `vela preview` shows a correct docker command.
- **DK1 DockerRuntime backend + lifecycle (~2–3d):** agent launch/supervise (run -d + logs -f + wait), docker sidecar + verify-before-act, stop/kill/restart, error classification. *Done when:* `test_docker_lifecycle`/`test_docker_errors` green against fake docker.
- **DK2 Logs/health/phase/discover/reattach (~1.5d):** LogSink streaming + scrub, health/reachable-url, FSM over container logs, discover/reattach by id+digest. *Done when:* `test_docker_logs_scrub`/`_health_phase`/`_discover_reattach` green.
- **DK3 Eviction + port guard + standalone export + docs (~1d):** sibling eviction, port preflight, `docker run` export, docs. *Done when:* eviction/guard tested; export reproduces the run.
- **DK4 Composer integration + real-hardware validation (~1d):** wire `runtime: docker` into the composer (composer spec DC4); run the real Blackbird Qwen3.6 FP8/BF16 native-docker deployment end-to-end and record an artifact. *Done when:* the Blackbird lane reaches READY via native docker and a validation artifact is committed.

**MVP = DK0–DK2** (launch/monitor/stop a vLLM container natively). **Full v1 = DK0–DK4.** Land DK0–DK1 alongside composer DC0–DC2 so the wizard's Docker option is real.

---

## Appendix A — Verified vLLM Docker facts & sources
- **Official image / run / `--ipc=host` / cache volume / `HF_TOKEN` / `--gpus`:** vLLM docs "Using Docker" (`docs.vllm.ai/.../deployment/docker/`) — `docker run --runtime nvidia --gpus all -v ~/.cache/huggingface:/root/.cache/huggingface --env HF_TOKEN=… -p 8000:8000 --ipc=host vllm/vllm-openai:<tag> --model …`; "use `ipc=host` or `--shm-size` … PyTorch shared memory, particularly tensor-parallel."
- **Pin tags/digests, not `:latest`; `--shm-size` 16g/32g; mount HF cache; `--gpus '"device=0,1"'`; CUDA_VISIBLE_DEVICES isolation; start-period 120–600s; SIGTERM graceful stop; `restart: unless-stopped` for self-managed (Vela uses `--restart no` + supervision):** 2026 production guides (PremAI "LLM Docker Deployment 2026"; Vultr ROCm production deployment; Inference.net vLLM Docker; Latitude Dockerizing-LLM checklist). Non-root + secrets-not-in-image + digest-pinning are the common security gaps these flag.
- **Lab confirmation:** `scripts/blackbird_qwen36_*_vllm_foreground.sh` already use `vllm/vllm-openai@sha256:b13d6e5…`, `--ipc=host`, `--shm-size 32g`, `--gpus all`, `--network host`, HF-cache mounts, `docker logs -f` + `docker wait`, graceful stop — i.e. this spec **codifies the lab's proven shape** into a Vela-owned runtime.

> No code written in this document.
