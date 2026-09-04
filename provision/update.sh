#!/usr/bin/env bash
# One pull-based convergence pass (ADR 0001 §2): ff-only merge of
# origin/main, reinstall on dependency change, sync systemd user units,
# restart only changed units. Runs unprivileged as `agent` from
# agent-ops-update.timer; shares <state>/convergence.lock with the
# dispatcher pass so code never swaps mid-pass.
#
# macOS has no flock(1), so the lock is taken by a python3 child on the
# inherited fd 9 — the lock lives on the shared open file description
# and survives the child's exit.
set -euo pipefail

REPO_DIR="${AGENT_OPS_REPO:-$HOME/agent-ops}"
STATE_DIR="${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}"
UNIT_DIR="${AGENT_OPS_UNIT_DIR:-$HOME/.config/systemd/user}"
SYSTEMCTL="${AGENT_OPS_SYSTEMCTL:-systemctl --user}"
PODMAN="${AGENT_OPS_PODMAN:-podman}"
PNPM="${AGENT_OPS_PNPM:-pnpm}"
HERDR="${AGENT_OPS_HERDR:-$HOME/.local/bin/herdr}"
HERDR_INSTALL="${AGENT_OPS_HERDR_INSTALL:-curl -fsSL https://herdr.dev/install.sh | sh}"

mkdir -p "$STATE_DIR" "$UNIT_DIR"

exec 9>"$STATE_DIR/convergence.lock"
python3 - <<'PY'
import fcntl, os, sys, time
deadline = time.monotonic() + float(os.environ.get("AGENT_OPS_FLOCK_WAIT", "300"))
while True:
    try:
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(1)
PY

cd "$REPO_DIR"
git fetch origin main
old=$(git rev-parse HEAD)
new=$(git rev-parse origin/main)
frontend_changed=0

if [ "$old" != "$new" ]; then
  git merge --ff-only origin/main

  if ! git diff --quiet "$old" "$new" -- pyproject.toml; then
    .venv/bin/pip install -e .
  fi

  if ! git diff --quiet "$old" "$new" -- Containerfile; then
    $PODMAN build -t agent-ops-session -f Containerfile .
  fi

  if ! git diff --quiet "$old" "$new" -- frontend/; then
    frontend_changed=1
  fi

  # The web console is a long-running service on an editable install: new
  # code goes live only on restart. Restart whenever the code it imports
  # changed (web/ itself, dispatcher/ and telegram/ it imports, or deps).
  if ! git diff --quiet "$old" "$new" -- web/ dispatcher/ telegram/ pyproject.toml; then
    $SYSTEMCTL try-restart agent-ops-web.service
  fi
fi

# --- herdr: the session layer's server binary -------------------------------
# A box provisioned before the herdr migration has no binary, yet the pass
# above has already pulled code that needs one. Unlike pnpm (warn and skip)
# the converging move is to install: the official installer is idempotent,
# sudo-free and writes only under $HOME. A failed install fails the pass
# here — before the unit sync, so no unit is restarted onto a box that
# cannot run sessions. No version pin: whatever the installer resolves;
# upgrades are an operator's `herdr update`.
if [ ! -x "$HERDR" ]; then
  echo "agent-ops update: herdr not found at $HERDR — installing" >&2
  HERDR_INSTALL_DIR="$(dirname "$HERDR")" sh -c "$HERDR_INSTALL"
  [ -x "$HERDR" ] || { echo "agent-ops update: herdr install did not produce $HERDR" >&2; exit 1; }
fi

# --- tmux → herdr, once ------------------------------------------------------
# Sessions started by the pre-herdr dispatcher live in tmux. Hand each to
# the park/resume path so the next pass resumes it in herdr (`claude
# --continue`; one interrupted turn per in-flight task, once). Guarded on
# an agent-ops-shaped session actually existing — NOT on a tmux server
# being up: an operator's own `tmux` as `agent` would otherwise re-run the
# migration on every pass, forever. So this is a no-op the moment no
# `task-*`/`triage` session remains, and a pure deletion once tmux leaves
# the box (with dispatcher/tmux_migration.py and the tmux apt line in
# bootstrap.sh).
TMUX="${AGENT_OPS_TMUX:-tmux}"
if command -v "$TMUX" >/dev/null 2>&1 && "$TMUX" ls -F '#{session_name}' 2>/dev/null \
     | grep -Eq '^(task-.*|triage)$'; then
  .venv/bin/python -m dispatcher.main --config "$STATE_DIR/targets.yaml" --migrate-tmux
fi

# Frontend build runs outside the rev-delta block for the same reason unit
# sync does: convergence repairs actual state. `frontend/dist` missing means
# web/app.py is serving the "ui": "not built" stub — a host cloned at HEAD
# (old==new on its very first pass) or one whose dist was cleared by hand
# would otherwise never get a UI. Build on a pulled frontend/ change OR on a
# missing dist.
if [ -d frontend ] && { [ "$frontend_changed" = 1 ] || [ ! -d frontend/dist ]; }; then
  # pnpm arrives via bootstrap.sh (corepack), which is one-shot: hosts
  # provisioned before the frontend existed have none. `set -e` would make a
  # missing pnpm abort the whole pass — taking unit sync and claude-home sync
  # with it, every timer firing, until someone SSHes in. Skip loudly instead.
  if command -v "$PNPM" >/dev/null 2>&1; then
    (cd frontend && $PNPM install --frozen-lockfile && $PNPM build)
    $SYSTEMCTL try-restart agent-ops-web.service
  else
    echo "agent-ops update: pnpm not found ($PNPM) — skipping frontend build;" \
         "run 'corepack enable pnpm' or re-run provision/bootstrap.sh" >&2
  fi
fi

# Unit sync runs even when HEAD is already at origin/main: convergence
# repairs actual unit-dir drift (manual pulls, interrupted passes), not
# just rev deltas.
changed=""
for src in provision/*.service provision/*.timer; do
  unit=$(basename "$src")
  if ! cmp -s "$src" "$UNIT_DIR/$unit"; then
    cp "$src" "$UNIT_DIR/$unit"
    changed="$changed $unit"
  fi
done

if [ -n "$changed" ]; then
  $SYSTEMCTL daemon-reload
  for unit in $changed; do
    # try-restart restarts long-running services and live timers. Two kinds
    # of unit are copied and daemon-reloaded above but never restarted here:
    #
    # Template units (`foo@.service`): systemd rejects `try-restart` without
    # an instance name, and under `set -euo pipefail` that non-zero exit
    # would abort the pass — the glob sorts agent-ops-alert@.service first,
    # so the pass would die before the remaining restarts, the credential
    # convergence and the claude-home sync, silently (no OnFailure here).
    #
    # Oneshots (`Type=oneshot`: the dispatcher, triage, sweep, digest,
    # keepalive and this updater): each timer firing already runs the
    # current checkout under the reloaded unit, so a restart buys nothing,
    # and restarting a RUNNING oneshot from here deadlocks — this script
    # holds convergence.lock for the whole pass, the replacement dispatcher
    # pass blocks in pass_lock on that same file, and try-restart waits for
    # it. On 2026-09-03 that hung the deploy for ten hours, stopped both
    # timers, and left the next merge unpulled. Skipping oneshots also
    # retires the old self-restart edge case for agent-ops-update.service.
    # Restarting agent-ops-update.timer still reschedules the timer, which
    # is harmless: the running pass is not its child.
    case "$unit" in *@.service) continue ;; esac
    if grep -q '^Type=oneshot' "$UNIT_DIR/$unit"; then continue; fi
    $SYSTEMCTL try-restart "$unit"
  done
fi

# --- credentials: converge freshest login into claude-home -------------------
# Spec 2026-07-31-auth-resilience §4: a manual `claude` login defaults to
# ~/.claude, but the fleet (containers, budget check, keepalive) reads
# claude-home. Copy the host store over claude-home's when it is strictly
# newer AND carries a token — a wrong-store login self-heals within one
# updater pass. Corrupt or tokenless files are never copied.
HOST_CREDS="${AGENT_OPS_HOST_CREDS:-$HOME/.claude/.credentials.json}"
CH_CREDS="$STATE_DIR/claude-home/.credentials.json"
if [ -f "$HOST_CREDS" ] \
   && { [ ! -f "$CH_CREDS" ] || [ "$HOST_CREDS" -nt "$CH_CREDS" ]; } \
   && python3 - "$HOST_CREDS" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    ok = isinstance(d, dict) and bool(d.get("claudeAiOauth", {}).get("accessToken"))
except Exception:
    ok = False
sys.exit(0 if ok else 1)
PY
then
  mkdir -p "$STATE_DIR/claude-home"
  # Temp-then-rename on the same filesystem (same pattern as
  # claude-home-sync.sh): the dispatcher, the keepalive and every session
  # container may be reading this file right now, and an in-place copy lets
  # a reader observe it truncated. `install -m 600` creates the temp with
  # the final mode, so the rename publishes a complete 600 file atomically.
  # Two plain statements so a failed `install` aborts the pass under set -e
  # (an AND-list silences non-final failures). A temp left behind by a
  # failed `mv` is harmless — the next pass overwrites it.
  install -m 600 "$HOST_CREDS" "$CH_CREDS.tmp"
  mv -f "$CH_CREDS.tmp" "$CH_CREDS"
  echo "agent-ops update: converged fresher host credentials into claude-home"
fi

# Claude-home convergence (ADR 0003): like unit sync, runs every pass to
# heal drift, not just on rev deltas. A failure fails the pass (set -e) —
# that is the loud surfacing channel for e.g. a pin mismatch.
bash provision/claude-home-sync.sh

# Credentials convergence: bootstrap runs credentials.sh once, so a box
# provisioned before a credentials feature merged (e.g. git commit signing)
# never picks it up. Re-run whenever the script's content differs from the
# last converged run. The stamp is written only on success, so a transient
# failure (1P outage) fails the pass loudly and retries next firing. A box
# whose single manual secret is not yet placed has nothing to materialize —
# skip loudly instead of failing every firing.
if [ -f provision/credentials.sh ]; then
  cred_sum=$(git hash-object provision/credentials.sh)
  cred_stamp="$STATE_DIR/credentials-converged.sha"
  if [ ! -f "$STATE_DIR/op-token.env" ]; then
    echo "agent-ops update: op-token.env missing — skipping credentials convergence" >&2
  elif [ "$(cat "$cred_stamp" 2>/dev/null || true)" != "$cred_sum" ]; then
    bash provision/credentials.sh
    printf '%s\n' "$cred_sum" > "$cred_stamp"
  fi
fi
