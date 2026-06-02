#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/rsync_to_gpu.sh USER@GPU_HOST:/absolute/remote/path

Copies this Mac working tree to a GPU box for real runtime tests.

Example:
  scripts/rsync_to_gpu.sh blackbird:/srv/lab-tui

Notes:
  - The destination must include a colon, using normal rsync SSH syntax.
  - Large/local-only folders such as .venv, caches, and run artifacts are excluded.
  - Secrets are not copied from shell environment; keep machine-specific tokens on the GPU host.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -ne 1 ]]; then
  usage
  exit $([[ $# -eq 1 ]] && [[ "${1:-}" =~ ^- ]] && echo 0 || echo 2)
fi

destination="$1"
if [[ "$destination" != *:* ]]; then
  echo "Destination must look like USER@HOST:/absolute/path" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

rsync -az --delete \
  --exclude '.git/' \
  --exclude '.mypy_cache/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude 'dist/' \
  --exclude 'build/' \
  --exclude '*.egg-info/' \
  --exclude 'runs/' \
  --exclude '.DS_Store' \
  "$repo_root/" "$destination/"

echo "Synced $repo_root to $destination"
