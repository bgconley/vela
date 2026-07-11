# Builds And Models

Builds and models are both target-owned resources surfaced over RPC, but they
have different storage semantics.

Builds are managed venvs under the target data dir. Models are metadata entries
over the shared Hugging Face cache or user-owned local paths; the loader does
not copy weights into an app-owned tree by default.

## Build Methods

| Method | Use | Installer behavior |
| --- | --- | --- |
| `pip` | Stable release such as `vllm==0.11.2` | Uses `uv` when available, otherwise Python venv plus pip. |
| `nightly` | Latest nightly wheel channel | nightly and commit require uv on the target. |
| `commit` | Exact vLLM commit wheel feed | nightly and commit require uv on the target. |
| `git` | Source checkout and editable install | Uses `uv` when available, otherwise Python venv plus pip. |
| `wheel` | Local wheel file | Uses `uv` when available, otherwise Python venv plus pip. |
| `adopt` | Existing external venv | Verifies the venv before registering it. |

Examples:

```bash
vela build add --target blackbird --method pip --spec 'vllm==0.11.2' --label stable-0112
vela build add --target blackbird --method nightly --channel cu130 --label nightly-cu130
vela build add --target blackbird --method git --url https://github.com/vllm-project/vllm.git --ref main
vela build adopt /agent/venvs/vllm-nightly --target blackbird --label adopted-nightly
vela build select stable-0112 --target blackbird
```

Operational commands:

```bash
vela build verify stable-0112 --target blackbird
vela build repair stable-0112 --target blackbird
vela build run stable-0112 --target blackbird -- python -c 'import vllm; print(vllm.__version__)'
vela build adopt /agent/venvs/vllm-nightly --target blackbird --label copied-nightly --copy
```

`build run` executes the command through the selected target agent with the
build's environment overlay, so the controller does not need the target venv
path locally. `build repair` regenerates the managed launcher artifacts
(`bin/`, `run.sh`, `activate`) from an existing manifest without reinstalling
vLLM. `build adopt --copy` copies an external venv into the managed build
directory before registration; without `--copy`, the registry references the
external path.

Managed builds write `build.json`, `install.log`, `bin/vllm`, `bin/python`,
`activate`, and `run.sh`. `install.log` is mode `0600`; output is scrubbed
before wire events and before durable log persistence.

Build removal has no --force override: it refuses live verified sidecar usage
and refuses config pins. Model removal --force only overrides config-pin protection;
live-run protection cannot be overridden.

## Model Registry

The model registry indexes:

- Hugging Face repo pins and scanned cached revisions.
- User-owned local model directories.
- URL entries that are launch-time-only unless a future fetcher is added.

Examples:

```bash
vela model pin tiny-llama \
  --target blackbird \
  --repo-id hf-internal-testing/tiny-random-LlamaForCausalLM \
  --revision main
vela model download tiny-llama --target blackbird
vela model download tiny-llama --target blackbird --json
vela model verify tiny-llama --target blackbird
vela model verify tiny-llama --target blackbird --deep
vela model remove tiny-llama --target blackbird
```

A Hugging Face pin with no explicit `--display-name` defaults its display name
to the repo id, and `model_ref` resolves against a **unique** repo id as well as
the entry id, display name, and aliases (an ambiguous repo id lists the
candidate entry ids so you can disambiguate). So `vela model pin org/repo`
followed by `model_ref: org/repo` in a config just works.

Re-pinning the same repo id (and revision) **upserts the existing entry in
place** — it keeps the entry id and refreshes the commit sha and metadata
instead of minting a duplicate — so the "re-pin the model" launch remediation
repairs an existing config's `model_ref` rather than stranding it. Pass `--new`
to force a fresh entry. If several entries already pin the repo, the pin refuses
and lists them (no guessing) — remove the duplicates or use `--new`.

For gated repos, accept the license upstream and set `HF_TOKEN` on the target
host before pinning or downloading. The token is never stored in the registry
and is scrubbed from job output.

`model download --json` emits the final job payload for automation. `model
verify --deep` runs the registry's deep content verification path when the
source supports it; shallow verification is still the default for quick health
checks.

Pre-downloading pays off for Docker deployments: a composed `runtime: docker`
deployment of a Hugging Face model **mounts the agent HF cache by default**
(`command.docker.hf_cache`), so the container reads the same cache
`vela model download` fills instead of re-downloading the weights on first
launch. A hand-written Docker config that omits the mount launches with a
`docker-no-hf-cache-mount` warning (see `docs/docker-runtime.md`).

Removal refuses live server usage and config pins. Actual reclaim for Hugging
Face revisions is dedup-aware because the registry delegates to the HF cache
APIs rather than deleting arbitrary files.

## Launch Composition

A launch is:

```text
target x build x model@revision x config
```

The agent resolves the selected build into executable, Python, environment, and
profile metadata. It resolves the selected model into a model argument,
revision flag, tokenizer override, and HF environment contribution. The
controller only passes ids and renders the resulting events.

### Cache check and registry learning

When a launch references a pinned Hugging Face model (`model_ref`) whose registry
entry is not yet `cached`, the agent surfaces a structured `model-not-cached`
warning (with the entry id and the download size when known) in the prepare/launch
result. The TUI renders it on the launch banner and `vela run`/`vela smoke` print
it to stderr. This is a warning by default so existing lab flows are not broken:
vLLM will simply download the weights during startup, which can silently consume
`launch.ready_timeout_seconds`.

Set `launch.require_cached_models: true` (config `launch:` block) or pass
`--require-cached` to `vela run`/`vela smoke`/`vela smoke-tui` to upgrade the
warning to a hard preflight failure (`model-not-cached`) before anything spawns.
A bare `model:` config with no `model_ref` cannot be checked against the registry,
so `require_cached_models` only warns for unpinned models — it never fails them
(an unpinned model is a deliberate escape hatch).

When a launch whose pinned model was not cached reaches READY, the agent runs a
full registry refresh (the same scan as `vela model refresh`) off its event
loop, so the registry learns that vLLM has now cached the weights.
Already-cached launches skip the refresh entirely. The refresh is best-effort: a
slow or failing scan never disturbs the running server.
