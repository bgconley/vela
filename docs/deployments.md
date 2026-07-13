# Deployments

The Vela TUI is the primary deployment composer. Open `vela`, press `n`, then
review the target, runtime, model, flags, preview, save, and smoke steps before
the agent writes a launchable config on the target host.

The CLI mirrors the same agent-side composer for automation, export, and CI, but
it should not be the mental model for everyday use. The controller asks the
target agent to compose, validate, save, launch, probe, stop, and stream events;
the controller never shells into Docker or dereferences target-local paths.

New profiles have an immutable runtime-identity gate before Review and again
before Save:

- Process profiles must select a target build. The picker shows the human label
  but saves the agent-returned `build_id`; Create build and Adopt venv also must
  return that id before the wizard can resume.
- Docker profiles must name a complete repo digest ending in
  `@sha256:<64 hex characters>`. Tags and abbreviated digests cannot be saved.

The schema remains able to read older bare-process, executable, and tagged-image
YAML so operators can inspect and migrate it; compatibility does not make those
mutable identities valid inputs for a newly composed profile.

## Blackwell Recipes

Known Blackbird deployments are local Blackwell recipe entries. The local
Blackwell recipe controls the pinned vLLM image digest, CUDA architecture
setting, FlashInfer/CUTLASS-sensitive backend choices, cache mounts, and FP8 or
BF16 memory shape.

Hugging Face metadata is advisory for model identity, revisions, gating, and
broad engine hints. It must not replace a local Blackwell recipe when selecting
the Docker image, vLLM build, FlashInfer layout, FlashAttention/CUTLASS shape, or
KV-cache sizing.

Current Blackbird recipes are available from the TUI recipe picker and from
`list_deployment_recipes`:

- `blackbird-qwen36-27b-fp8-rp6000`
- `blackbird-qwen36-27b-bf16-rp6000`

Each recipe payload includes `source_artifacts` entries pointing to the local
deployment script and run record that justify the vLLM image, backend, cache, and
memory shape. The Qwen3.6 Blackbird recipes currently record the proven image
stack as vLLM `0.20.2rc1.dev9+g01d4d1ad3`, Transformers `5.7.0`, Torch
`2.11.0+cu130`, and CUDA `13.0`; `version_profile: current` remains only the
Vela flag-compatibility profile.

The legacy `scripts/blackbird_qwen36_*_foreground.sh` wrappers remain as recipe
provenance and manual comparison tools. Active Vela configs should use native
`command.runtime: docker`.

## Review And Smoke

The wizard's review step shows the generated config, masked command preview,
derived fields, warnings, and validation results. Save writes the config on the
target. Save & Smoke then performs a bounded launch, waits for READY, stops the
run, and returns the TUI to the saved deployment instead of leaving a long-running
tail attached.
