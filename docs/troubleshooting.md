# Vela troubleshooting

[Documentation home](index.md) · [Getting started](getting-started.md) ·
[Operations](operations.md) · [CLI reference](cli-reference.md)

This guide maps the errors Vela prints to a cause and an exact fix. Each section
is one **remediation kind** — the `KIND` token in the CLI/TUI banner. The CLI
renders remediable errors as:

```
ERROR <KIND>: <message>
<fix line>
```

so the `KIND` after `ERROR ` tells you which section below to read. The kinds
come from `src/vela/remediation.py`; the launch/discovery sections after them
cover the Phase-5/6/7 surfaces that are their own canonical answer. Error text is
quoted verbatim from the code — placeholders like `<target>` are substituted at
runtime.

Throughout, `<target>` is the target name (`local` by default, or whatever you
passed to `--target` / set with `vela targets use` / `VELA_TARGET`).

---

## AGENT_NOT_INSTALLED

**Symptom**

```
ERROR AGENT_NOT_INSTALLED: Target agent command not found: vela
Fix: run `vela targets bootstrap <target> --install`.
```

**Cause** — the target host has no `vela` agent on its PATH or in its managed
venv, so the controller cannot start or reach an agent there.

**Fix** — provision the agent over SSH:

```bash
vela targets bootstrap <target> --host user@host --install
```

`--install` installs the agent into the target's managed venv; drop it if the
agent is already present and only needs registering.

---

## AGENT_VERSION_MISMATCH

**Symptom**

```
ERROR AGENT_VERSION_MISMATCH: <message>
Fix: run `vela targets bootstrap <target> --install` to upgrade the target agent.
```

For a **local** socket daemon this usually surfaces first as the stale-daemon
warning banner, which ends with `restart with: vela agent restart` (or, when the
daemon is provably newer than the controller, advises upgrading the controller).

**Cause** — the target agent and the controller disagree on the RPC protocol
version. A long-running local daemon can serve month-old code after an upgrade
because it is not restarted automatically.

**Fix** — upgrade the target agent, or restart a stale local daemon:

```bash
vela targets bootstrap <target> --install   # remote target
vela agent restart                          # local daemon
```

---

## AGENT_UNREACHABLE

`AGENT_UNREACHABLE` has two shapes — a local daemon that will not answer, and an
SSH hop that fails.

### Local daemon

**Symptom**

```
ERROR AGENT_UNREACHABLE: <message>
Fix: check `vela agent status`; the local agent log is at <runtime>/agent-start.err.
```

**Cause** — the local Unix-socket daemon is not running, or it crashed during
startup (a bad socket path, a bind error, or a broken import).

**Fix** — inspect status and the captured startup stderr:

```bash
vela agent status
cat <runtime>/agent-start.err    # the path the banner printed
```

A cleanly-stopped daemon removes `agent-start.err`; a failed start leaves it in
place so the reason survives. `vela agent restart` re-spawns the daemon.

### SSH target

**Symptom** — the cause names the SSH failure and an `SSH stderr:` line follows:

```
ERROR AGENT_UNREACHABLE: SSH auth failed
SSH stderr: <ssh output>
Fix: run `vela targets setup-ssh <target>`.
```

The cause is one of `SSH auth failed`, `SSH host key verification failed`,
`SSH host name did not resolve`, `SSH connection failed`, or `SSH failed`.

**Cause** — the controller could not open an SSH session to the target host.

**Fix**

```bash
vela targets setup-ssh <target>
```

Prefer a key available on the controller or a `ProxyJump`; agent forwarding is
blocked on the controller-to-agent transport.

---

## AGENT_AUTH_REQUIRED

**Symptom**

```
ERROR AGENT_AUTH_REQUIRED: target agent requires a valid capability token
Fix: run `vela agent gen-token --install --target <target>`.
```

**Cause** — the target agent is configured to require a capability token
(`VELA_AGENT_TOKEN` / `VELA_AGENT_REQUIRE_TOKEN`) and the controller does not have
a matching one.

**Fix** — generate and install a token on both sides:

```bash
vela agent gen-token --install --target <target>
```

---

## AGENT_TOKEN_MALFORMED

**Symptom**

```
ERROR AGENT_TOKEN_MALFORMED: controller agent token is malformed
Fix: run `vela agent gen-token --install --target <target>`.
```

**Cause** — the controller's configured `VELA_AGENT_TOKEN` is malformed (empty,
whitespace, or below the minimum entropy). A configured token must be a single
non-whitespace value with at least 128 bits of entropy.

**Fix** — regenerate a strong token:

```bash
vela agent gen-token --install --target <target>
```

---

## UV_REQUIRED

**Symptom**

```
ERROR UV_REQUIRED: uv is required for this build method
Fix: run `vela build doctor --target <target>`; install uv on the target or choose pip, wheel, or git.
```

**Cause** — the `nightly` and `commit` build methods need `uv` on the target,
because pip cannot enforce the index-priority semantics those wheel feeds require.

**Fix** — install `uv` on the target, or pick a method that falls back to
Python venv + pip:

```bash
vela build doctor --target <target>
```

---

## Gated model / missing HF_TOKEN

**Symptom** — at preflight, for a repo that requires accepting a license:

```
model <name> requires HF_TOKEN; accept the model license and set HF_TOKEN (agent env or config env: block)
```

At runtime, a `401`/`403` from the model endpoint is classified as `HF_AUTH`
rather than a healthy READY.

**Cause** — the pinned repo is gated and the target has no `HF_TOKEN`, so the
weights cannot be downloaded or served.

**Fix** — accept the model license on Hugging Face, then set `HF_TOKEN` on the
target: either in the agent environment or in the config's `env:` block. Keep the
token on the target host; tokens are scrubbed before job output leaves the agent.

---

## model-not-cached (launch will silently download)

**Symptom** — a launch warning (or a hard failure with `--require-cached`):

```
model <name> (<entry_id>) is not cached (cache_state=<state>); vLLM will download it during startup, which can silently consume the ready timeout
```

**Cause** — the model's weights are not in the target's Hugging Face cache, so
vLLM would download them during startup — which can quietly burn the whole ready
timeout and look like a hang.

**Fix** — download the weights first, then launch:

```bash
vela model download <ref> --target <target>
```

To turn the warning into a hard pre-launch gate, set
`launch.require_cached_models: true` in the config (or pass `--require-cached` to
`vela run`/`vela smoke`). An uncached, unpinned bare model is only warned, never
gated.

---

## insufficient disk (DISK_FULL / insufficient-disk)

**Symptom**

```
insufficient disk for model download: need ~<size> (download size + 10% headroom) but only <free> free (<pct>% of required)
```

The launch preflight classifies this as `DISK_FULL`; a model download reports the
`insufficient-disk` kind.

**Cause** — the resolved Hugging Face cache directory does not have the download
size plus a 10% headroom free (the disk-headroom precheck).

**Fix** — free space on that volume, or point the cache at a larger one (for
example set `HF_HOME` on the target, or mount a bigger volume for docker
deployments). Re-run once there is room.

---

## image-pull-timeout (docker)

**Symptom**

```
docker pull for <image> exceeded <N>s; raise VELA_DOCKER_PULL_TIMEOUT_SECONDS or pre-pull the image on the target
```

**Cause** — a `docker run` deployment's image pull exceeded the timeout. A real
vLLM image is large (~10 GB); the default limit is 1800 seconds.

**Fix** — raise the timeout on the target agent, or pre-pull the image:

```bash
export VELA_DOCKER_PULL_TIMEOUT_SECONDS=3600   # on the target agent; <=0 disables the limit
docker pull <image>                            # or pre-pull on the target
```

---

## Unknown config (searched dirs / daemon cwd)

**Symptom**

```
ERROR: Unknown config: <name>
Searched (agent '<target>', cwd <cwd>): <dir1>, <dir2>
```

**Cause** — the named config was not found in any directory the target agent
searched. A daemon started from a different working directory will not see a
relative `./configs`, which is the most common surprise.

**Fix** — list what the agent can actually see, then point it at the right place:

```bash
vela list --target <target>
```

Pass `--configs-dir <dir>`, set `VELA_CONFIGS`, put the config in
`~/.config/vela/configs` (the `configs/` subdir, honoring `XDG_CONFIG_HOME`), or
restart the daemon from the directory that holds `./configs`.

---

## Other preflight failures

Preflight also classifies problems it can name precisely; each prints as
`ERROR <KIND>: <detail>` with the detail spelling out the fix:

- `PORT_IN_USE` — the configured `server.port` is already bound. Free it or change
  the port.
- `TP_MISMATCH` — `tensor_parallel_size × pipeline_parallel_size` exceeds the
  visible `CUDA_VISIBLE_DEVICES`. Fix the world size or the device list.
- `COMMAND_NOT_FOUND` — the launch executable is missing; install vLLM or set
  `command.entrypoint: module`.
- `MODEL_NOT_FOUND` — a local model path does not exist on the target.
- `CONFIG_INVALID` — a `vllm.require_flags` gate failed for the resolved build.

Run `vela run <config> --preview --target <target>` to see the resolved command
and warnings without launching anything.

## Related documentation

- [Getting started and Doctor](getting-started.md)
- [Day-two operations and recovery](operations.md)
- [Environment variables and storage paths](environment.md)
- [Complete CLI reference](cli-reference.md)
