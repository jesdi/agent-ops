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

# --- claude: long-lived OAuth token -> claude-token.env ---------------------
# `claude setup-token` output, saved to 1P (item agent-ops-claude, field
# CLAUDE_CODE_OAUTH_TOKEN). Materialized as an env file the units load via
# EnvironmentFile and the session containers via podman --env-file — a
# static token with no refresh step, so nothing races the single-use
# refresh token in claude-home/.credentials.json. Absence is fine: the
# fleet then falls back to the shared OAuth store as before.
if tok=$($OP read "op://agent-ops/agent-ops-claude/CLAUDE_CODE_OAUTH_TOKEN" \
    2>/dev/null); then
  umask 077
  printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$tok" > "$STATE_DIR/claude-token.env"
  echo "claude long-lived token restored to $STATE_DIR/claude-token.env"
else
  echo "no long-lived claude token in 1P — run 'claude setup-token', save the"
  echo "token to 1P item agent-ops-claude field CLAUDE_CODE_OAUTH_TOKEN, then"
  echo "re-run this script"
fi

# --- git: SSH commit signing ------------------------------------------------
# Signing is only enabled once the key materializes; setting commit.gpgsign
# with no key on disk would break every commit on the box.
if key=$($OP read \
    "op://agent-ops/agent-ops-git-signing/private key?ssh-format=openssh" \
    2>/dev/null); then
  umask 077
  printf '%s\n' "$key" > "$STATE_DIR/git-signing-key"
  $OP read "op://agent-ops/agent-ops-git-signing/public key" \
    > "$STATE_DIR/git-signing-key.pub"
  git config --global gpg.format ssh
  git config --global user.signingkey "$STATE_DIR/git-signing-key"
  git config --global commit.gpgsign true
  git config --global tag.gpgsign true
  echo "git signing key restored to $STATE_DIR/git-signing-key"
else
  echo "no git-signing key in 1P (item agent-ops-git-signing) — commits stay unsigned"
fi
