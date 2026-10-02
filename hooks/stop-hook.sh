#!/usr/bin/env bash
# Session-stop hook, installed into task worktrees by workspace.py. It is
# both Claude Code's Stop hook and Codex's `notify` program (fired on
# agent-turn-complete, run directly with a JSON argument, which it ignores;
# with an argument it never reads stdin). Every stop pings waitd. Claude (no
# argument) passes its hook input as JSON on stdin; when the stage signal says
# `working` and it lists background_tasks, the ping forwards them and waitd
# records a background wait; otherwise waitd writes a waiting marker and the
# dispatcher parks the task on its next pass.
# Must never fail the session, so: always exit 0.
#
# Self-locating: the CLI fires the hook with the session's current cwd,
# which is NOT guaranteed to be the worktree root (the agent may have left
# it in a subdir). Resolve task.json against this script's own directory
# (the .agent dir) instead of cwd, so the issue number is always read — a
# cwd-relative read would silently miss and the waiting ping would never
# fire, hanging the task unparked.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 0
ISSUE=$(python3 -c "import json;print(json.load(open('$HERE/task.json'))['issue'])" 2>/dev/null) || exit 0
# Old worktrees' task.json predates the target field — default to empty so
# they still ping (waitd reads an absent/empty target as a legacy ping).
TARGET=$(python3 -c "import json;print(json.load(open('$HERE/task.json')).get('target', ''))" 2>/dev/null) || TARGET=""
BG=""
if [ $# -eq 0 ]; then
  # Any unusable input (empty, garbage, non-list, missing stage.json) → BG stays empty.
  # python reads stdin itself: an env var or argument would hit the OS size
  # limit on a long hook input and silently fall back to a waiting ping.
  BG=$(python3 -c "
import json, sys
try:
    hook_in = sys.stdin.read()   # drain first, so the CLI never writes into a closed pipe
    if json.load(open('$HERE/stage.json')).get('status') != 'working':
        raise ValueError
    bg = json.loads(hook_in).get('background_tasks')
    if isinstance(bg, list) and bg:
        print(json.dumps(bg))
except Exception:
    pass
" 2>/dev/null) || BG=""
fi
EXTRA=""
[ -n "$BG" ] && EXTRA=", \"background_tasks\": $BG"
SOCK="${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}/wait/wait.sock"
curl --silent --max-time 5 --unix-socket "$SOCK" \
  -X POST "http://localhost/waiting" \
  -H 'Content-Type: application/json' \
  -d "{\"issue\": $ISSUE, \"target\": \"$TARGET\"$EXTRA}" >/dev/null 2>&1 || true
exit 0
