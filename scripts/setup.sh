#!/usr/bin/env bash
# Get a fresh clone or a new worktree ready to work in: Python deps, frontend
# deps and the general-skills. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  # pyproject needs >= 3.11; a bare python3 is often the system 3.9. $PYTHON overrides.
  for py in ${PYTHON:-} python3.13 python3.12 python3.11 python3; do
    "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && break
    py=
  done
  [ -n "$py" ] || { echo "setup: need Python >= 3.11 (set PYTHON=...)" >&2; exit 1; }
  "$py" -m venv .venv
fi
.venv/bin/pip install -q -e '.[dev]'
(cd frontend && pnpm install --frozen-lockfile)
./scripts/install-skills.sh
