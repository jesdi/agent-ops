#!/usr/bin/env bash
# Materialize box credentials from 1Password. Run as `agent`; idempotent —
# safe to re-run any time (e.g. after rotating a token in 1P). The box's
# single manually-placed secret is op-token.env; everything here derives
# from it. Interactive fallbacks (first-ever claude login) are printed,
# never prompted.
set -euo pipefail

STATE_DIR=${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}
OP=${AGENT_OPS_OP:-op}
GH=${AGENT_OPS_GH:-gh}

TOKEN_FILE=$STATE_DIR/op-token.env
if [ ! -f "$TOKEN_FILE" ]; then
  echo "missing $TOKEN_FILE — write OP_SERVICE_ACCOUNT_AGENT_OPS_TOKEN=... there first" >&2
  exit 1
fi
set -a
# shellcheck source=/dev/null
. "$TOKEN_FILE"
set +a
export OP_SERVICE_ACCOUNT_TOKEN=$OP_SERVICE_ACCOUNT_AGENT_OPS_TOKEN

# --- gh: repo-scoped fine-grained PAT -> gh credential store ----------------
$OP read "op://agent-ops/agent-ops-github/GH_REPO_TOKEN" \
  | $GH auth login --with-token

# --- claude: best-effort restore of backed-up OAuth credentials -------------
# The interactive `claude` login is the source; if its .credentials.json was
# saved to 1P (item agent-ops-claude, field credentials.json), restore it so
# a rebuilt box needs no browser round-trip. Absence is fine on first setup.
if creds=$($OP read "op://agent-ops/agent-ops-claude/credentials.json" 2>/dev/null); then
  mkdir -p "$STATE_DIR/claude-home"
  umask 077
  printf '%s\n' "$creds" > "$STATE_DIR/claude-home/.credentials.json"
  echo "claude credentials restored to $STATE_DIR/claude-home/"
else
  echo "no claude credentials in 1P — run 'claude' interactively once, then:"
  echo "  cp -r ~/.claude/. $STATE_DIR/claude-home/"
fi
