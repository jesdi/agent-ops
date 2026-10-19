#!/usr/bin/env bash
# Get a fresh clone or a new worktree ready to work in: Python deps, frontend
# deps and the general-skills. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

# uv installs the Python of .python-version and the exact versions of uv.lock
# into .venv, the same set CI installs.
command -v uv >/dev/null || { echo "setup: needs uv (https://docs.astral.sh/uv/)" >&2; exit 1; }
uv sync --locked
(cd frontend && pnpm install --frozen-lockfile)
./scripts/install-skills.sh

# Committed hooks (.githooks/). post-checkout installs the skills in every new
# worktree, however it is created. The setting is shared by all worktrees.
git config core.hooksPath .githooks
