#!/usr/bin/env bash
# Create .worktrees/<name> on a new branch from the latest origin/<base>, then
# run setup in it, so the worktree never starts without its skills.
# Usage: scripts/new-worktree.sh <branch> [base]   (base defaults to main)
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 <branch> [base]" >&2; exit 1; }
branch=$1 base=${2:-main}
# The main checkout, even when run from inside another worktree.
cd "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"

dir=".worktrees/${branch//\//-}"
git fetch -q origin "$base"
git worktree add --no-track "$dir" -b "$branch" "origin/$base"
"$dir/scripts/setup.sh"
echo "ready: $dir"
