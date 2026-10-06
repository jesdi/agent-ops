#!/usr/bin/env bash
# Session-stop hook, installed into task worktrees by workspace.py. It is
# both Claude Code's Stop hook and Codex's `notify` program (fired on
# agent-turn-complete, run directly with a JSON argument, without reading
# stdin). The ping reports the conversation
# ID: Codex sends thread-id with runtime codex; Claude reads session_id from
# its hook input on stdin. waitd classifies Codex roots and subagents. When
# Claude's stage signal says `working` and it lists background_tasks, the
# ping forwards them and waitd
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
# Pass paths as arguments and serialize the whole ping, so quotes and
# backslashes in the task identity or conversation ID remain valid JSON.
PING=$(python3 -c '
import json
import sys
from pathlib import Path

here = Path(sys.argv[1])
task = json.loads((here / "task.json").read_text())
# Old worktrees lack target; waitd treats an empty target as a legacy ping.
ping = {"issue": task["issue"], "target": task.get("target", "")}
codex = len(sys.argv) > 2
# Argument mode never reads stdin. For Claude, drain stdin before parsing;
# large hook inputs must not pass through an env var or argument.
hook_in = sys.argv[2] if codex else sys.stdin.read()
try:
    hook_in = json.loads(hook_in)
except (ValueError, RecursionError):
    hook_in = {}
if not isinstance(hook_in, dict):
    hook_in = {}
session_id = hook_in.get("thread-id" if codex else "session_id")
if isinstance(session_id, str) and session_id:
    ping["session_id"] = session_id
if codex:
    ping["runtime"] = "codex"
else:
    try:
        stage = json.loads((here / "stage.json").read_text())
        bg = hook_in.get("background_tasks")
        if stage.get("status") == "working" and isinstance(bg, list) and bg:
            ping["background_tasks"] = bg
    except (OSError, ValueError, AttributeError, RecursionError):
        pass
print(json.dumps(ping))
' "$HERE" "$@" 2>/dev/null) || exit 0
SOCK="${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}/wait/wait.sock"
curl --silent --max-time 5 --unix-socket "$SOCK" \
  -X POST "http://localhost/waiting" \
  -H 'Content-Type: application/json' \
  -d "$PING" >/dev/null 2>&1 || true
exit 0
