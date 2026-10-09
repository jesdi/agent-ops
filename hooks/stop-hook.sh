#!/usr/bin/env bash
# Native Claude lifecycle and legacy session-stop hook. Prepared Claude launches
# forward native session/prompt identity plus their immutable launch environment;
# waitd owns binding validation, background inventory and the cap clock. Legacy
# launches retain the Stop/notify marker path below. Codex notify alone never
# supplies authoritative lifecycle evidence for a prepared launch.
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
import os
import sys
from pathlib import Path

here = Path(sys.argv[1])
codex = len(sys.argv) > 2
if os.environ.get("AGENT_OPS_LAUNCH_ID") and all(
        key in os.environ for key in ("AGENT_OPS_TARGET", "AGENT_OPS_ISSUE")):
    ping = {"issue": os.environ["AGENT_OPS_ISSUE"], "target": os.environ["AGENT_OPS_TARGET"]}
else:
    # Legacy/incomplete launch environments still use worktree task identity.
    # Real managed launches carry all identity fields and never read this file.
    task = json.loads((here / "task.json").read_text())
    ping = {"issue": task["issue"], "target": task.get("target", "")}
# Argument mode never reads stdin. For Claude, drain stdin before parsing;
# large hook inputs must not pass through an env var or argument.
hook_in = sys.argv[2] if codex else sys.stdin.read()
try:
    hook_in = json.loads(hook_in)
except (ValueError, RecursionError):
    hook_in = {}
if not isinstance(hook_in, dict):
    hook_in = {}
if os.environ.get("AGENT_OPS_LAUNCH_ID") and not codex:
    ping.update({key: hook_in[key] for key in
                 ("hook_event_name", "session_id", "prompt_id", "agent_id", "background_tasks")
                 if key in hook_in})
    for key in ("launch_id", "conversation_id", "stage", "ticket"):
        ping[key] = os.environ.get("AGENT_OPS_" + key.upper(), "")
    ping["target"] = os.environ.get("AGENT_OPS_TARGET", ping["target"])
    ping["issue"] = os.environ.get("AGENT_OPS_ISSUE", ping["issue"])
    print(json.dumps(ping))
    sys.exit(0)
# New input/health hooks on an unmanaged/manual session are not legacy Stops.
if not codex and hook_in.get("hook_event_name") in ("SessionStart", "UserPromptSubmit"):
    sys.exit(1)  # outer shell exits successfully without sending a ping
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
