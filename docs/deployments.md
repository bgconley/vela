# Deployments

The Vela TUI is the primary deployment composer. Open `vela`, press `n`, then
review the target, runtime, model, flags, preview, save, and smoke steps before
the agent writes a launchable config on the target host.

The CLI mirrors the same agent-side composer for automation, export, and CI, but
it should not be the mental model for everyday use. The controller asks the
target agent to compose, validate, save, launch, probe, stop, and stream events;
the controller never shells into Docker or dereferences target-local paths.

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
memory shape.

The legacy `scripts/blackbird_qwen36_*_foreground.sh` wrappers remain as recipe
provenance and manual comparison tools. Active Vela configs should use native
`command.runtime: docker`.

## Review And Smoke

The wizard's review step shows the generated config, masked command preview,
derived fields, warnings, and validation results. Save writes the config on the
target. Save & Smoke then performs a bounded launch, waits for READY, stops the
run, and returns the TUI to the saved deployment instead of leaving a long-running
tail attached.
