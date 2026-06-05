# Agent PA0: Local Target Foundation

## Goal

Begin the agent/controller refactor by making local execution use the same target vocabulary that remote execution will use. This slice does not introduce the daemon or SSH bridge yet; it creates the schema and registry primitives that later RPC and TUI routing will depend on.

## Scope

- Add an optional top-level `target` field to `ModelConfig`.
- Add a controller-local targets registry with an implicit, non-removable `local` target.
- Support SSH target metadata without using it for execution yet.
- Keep the existing config registry behavior and `extra="forbid"` validation.

## Test First

1. Prove `ModelConfig` accepts `target: blackbird`.
2. Prove a missing `targets.yaml` still yields an implicit `local` target.
3. Prove a YAML registry loads `ssh` targets while preserving `local` first.
4. Prove file entries cannot override the implicit `local` target.

## Implementation Steps

1. Extend `ModelConfig` with `target: str | None = None`.
2. Add `vela.config.targets` with `TransportKind`, `TargetConfig`, `TargetsRegistry`, `default_targets_path`, and `load_targets_file`.
3. Validate SSH targets require `host`.
4. Keep error handling strict for malformed target files so later UI/CLI layers can surface named failures cleanly.

## Verification

- Run the new focused tests.
- Run the existing config tests.
- Run `ruff check .` and the full pytest suite locally.

## Follow-up Slices

- PA0b: introduce `TargetClient` and an in-process local agent facade.
- PA0c: route CLI preview/list through the local target client.
- PA0d: route TUI launch/stop/status/event surfaces through the local target client.
