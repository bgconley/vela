# Changelog

All notable changes to Vela are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

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
- Test suite: 1087 hermetic tests (isolated XDG state, per-session agent
  daemon), golden-path journey coverage, ruff-clean.

[0.1.0]: https://github.com/bgconley/vela/releases/tag/v0.1.0
