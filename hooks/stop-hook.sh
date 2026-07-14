#!/usr/bin/env bash
# Claude Code Stop hook, installed into task worktrees by workspace.py.
# Fires whenever the session stops for input → pings waitd → Telegram
# "task #N is waiting". Mechanical: session stops ⇒ ping. Must never fail
# the session, so: always exit 0.
set -u
ISSUE=$(python3 -c 'import json;print(json.load(open(".agent/task.json"))["issue"])' 2>/dev/null) || exit 0
SOCK="${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}/wait.sock"
curl --silent --max-time 5 --unix-socket "$SOCK" \
  -X POST "http://localhost/waiting" \
  -H 'Content-Type: application/json' \
  -d "{\"issue\": $ISSUE}" >/dev/null 2>&1 || true
exit 0
