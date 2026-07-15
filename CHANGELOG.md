# Changelog

All notable changes to Vela are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Complete user documentation home with installation, concepts, a screenshot-led
  real deployment tutorial, day-two operations, environment/path reference, and
  generated full CLI reference.
- Provenance-tracked tutorial screenshot corpus sourced byte-for-byte from the
  checksummed July 13 Oxcart controller-and-target workflow.
- CLI and screenshot documentation generators plus drift tests for command
  coverage, links, images, and source evidence.
- Deployment clone/edit/delete, config push/pull/lint/edit, model inspect/adopt,
  build inspect/run/remove, run-log follow, and safe artifact pruning workflows.

### Changed

- New deployments require immutable process-build or Docker-image identity and
  show resolved model revision, field provenance, redacted command, mounts,
  exposure, and destructive behavior before save or smoke.
- Target/model/build/flag/config managers use responsive master-detail layouts;
  target connection work remains asynchronous so the UI stays operable during a
  dead SSH attempt.
- User docs now teach only canonical CLI spellings and distinguish saved
  profiles, transient Use once overrides, attached/detached runs, and release
  evidence from historical engineering plans.

### Fixed

- `deploy create --json` no longer saves a profile after failed preflight unless
  the operator explicitly passes `--force`; failing JSON lint, build verify,
  build repair, and model verify operations now also return nonzero automation
  exit codes.
- `targets bootstrap TARGET --install` can now repair an already registered SSH
  target without making the operator repeat its host and connection settings.
- Review rendering no longer interprets bracket-rich Docker JSON as Rich markup.
- Profile save conflicts return to an editable draft instead of losing work.
- Browser-served walkthrough ownership and attached-run identification are
  fail-closed, preventing stale UI/server or ambiguous-run cleanup.

## [0.1.0] - 2026-06-10

First tagged release.

### Added

- Phase-aware Textual TUI: dashboard (config sidebar, phase timeline, GPU
  panel, scrubbed live log), New Deployment wizard (6 steps with progressive
  disclosure, draft preservation, review + preflight checklist, save & smoke),
  and master-detail managers for targets, builds, models, and flags.
- Controller/agent architecture: a local or SSH-attached target agent owns
  process lifecycle, detached supervision (sidecar + manifest + reattach),
  durable scrubbed logs, health probing (including post-READY degradation
  detection), GPU sampling, build jobs, and model downloads.
- CLI surface: `vela` TUI plus `list/preview/run/smoke`, `targets`
  (add/bootstrap/test/setup-ssh), `build` (add/verify/select/repair/adopt/
  doctor), `model` (pin/download/verify/refresh), `deploy create/export`,
  `agent`, and `doctor`.
- Build methods: `pip`, `nightly`, `commit`, `git`, `wheel`, `adopt`
  (uv-backed where index-priority semantics are required).
- Model registry: HF cache-cataloged pins with revision/sha verification and
  gated-repo auth classification.
- Docker runtime: agent-generated `docker run`, container lifecycle, digest
  recording in the sidecar.
- Remote validation lane: `scripts/run_remote_tests.sh` + scheduled GitHub
  Actions workflow producing dated artifacts (latest green: full suite +
  live Qwen3.6-27B smoke on an RTX PRO 6000 Blackwell).
- Mypy remains incremental for v0.1.0; the accepted legacy debt is tracked in
  `docs/mypy-debt.md` and CI prevents the ignored-module list from growing.
- Test suite: 1087 hermetic tests (isolated XDG state, per-session agent
  daemon), golden-path journey coverage, ruff-clean.

[0.1.0]: https://github.com/bgconley/vela/releases/tag/v0.1.0
