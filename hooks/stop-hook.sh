#!/usr/bin/env bash
# Claude Code Stop hook, installed into task worktrees by workspace.py.
# Fires whenever the session stops for input → pings waitd → waiting marker
# → dispatcher parks on next pass. Mechanical: session stops ⇒ ping.
# Must never fail the session, so: always exit 0.
#
# Self-locating: Claude fires Stop hooks with the session's current cwd,
# which is NOT guaranteed to be the worktree root (the agent may have left
# it in a subdir). Resolve task.json against this script's own directory
# (the .agent dir) instead of cwd, so the issue number is always read — a
# cwd-relative read would silently miss and the waiting ping would never
# fire, hanging the task unparked.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 0
ISSUE=$(python3 -c "import json;print(json.load(open('$HERE/task.json'))['issue'])" 2>/dev/null) || exit 0
SOCK="${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}/wait.sock"
curl --silent --max-time 5 --unix-socket "$SOCK" \
  -X POST "http://localhost/waiting" \
  -H 'Content-Type: application/json' \
  -d "{\"issue\": $ISSUE}" >/dev/null 2>&1 || true
exit 0
