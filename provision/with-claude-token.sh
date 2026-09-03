#!/usr/bin/env bash
# Exec "$@" with CLAUDE_CODE_OAUTH_TOKEN resolved from 1P at spawn time.
# The long-lived token (claude setup-token; item agent-ops-claude, field
# CLAUDE_CODE_OAUTH_TOKEN) is deliberately never persisted on the box — it
# exists only in this process and the exec'd command's environment. Every
# failure mode degrades to plain exec: the wrapped claude then falls back
# to the shared claude-home OAuth store, i.e. today's behavior.
#
# Callers: session containers (herdr pane -> podman, via bare
# `-e CLAUDE_CODE_OAUTH_TOKEN` passthrough), triage containers, and the
# keepalive unit. herdr panes have no op auth of their own, so the wrapper
# sources op-token.env itself when needed.
set -u

STATE_DIR=${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}
OP=${AGENT_OPS_OP:-op}

if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] && [ -f "$STATE_DIR/op-token.env" ]; then
  # shellcheck source=/dev/null
  . "$STATE_DIR/op-token.env"
  export OP_SERVICE_ACCOUNT_TOKEN=${OP_SERVICE_ACCOUNT_AGENT_OPS_TOKEN:-}
fi

tok=""
if [ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
  # timeout: a wedged 1P read must not strand the spawn — a stage that
  # never starts is worse than one on the fallback store.
  tok=$(timeout 15 "$OP" read \
    "op://agent-ops/agent-ops-claude/CLAUDE_CODE_OAUTH_TOKEN" \
    2>/dev/null || true)
fi

if [ -n "$tok" ]; then
  CLAUDE_CODE_OAUTH_TOKEN=$tok exec "$@"
fi
exec "$@"
