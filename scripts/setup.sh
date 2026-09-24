#!/usr/bin/env bash
# Get a fresh clone or a new worktree ready to work in: Python deps, frontend
# deps and the general-skills. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -e '.[dev]'
(cd frontend && pnpm install --frozen-lockfile)
./scripts/install-skills.sh
