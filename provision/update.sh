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

if [ "$old" != "$new" ]; then
  git merge --ff-only origin/main

  if ! git diff --quiet "$old" "$new" -- pyproject.toml; then
    .venv/bin/pip install -e .
  fi

  if ! git diff --quiet "$old" "$new" -- Containerfile; then
    $PODMAN build -t agent-ops-session -f Containerfile .
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
    # try-restart: no-op for units that are not running (oneshots between
    # timer firings); restarts long-running services and live timers.
    #
    # KNOWN EDGE CASE — self-restart: if agent-ops-update.service or
    # agent-ops-update.timer is among the changed units, try-restarting it
    # here will kill or reschedule the running instance of this very script.
    # Any unit whose name sorts after the updater's own units in $changed will
    # not be restarted this pass.  This is accepted behaviour (plan-mandated
    # verbatim): the next timer firing will see old==new for those units and
    # skip them, so a co-changed service may run stale code for one deploy
    # cycle until its own file changes again.  No units are filtered here.
    $SYSTEMCTL try-restart "$unit"
  done
fi

# Claude-home convergence (ADR 0003): like unit sync, runs every pass to
# heal drift, not just on rev deltas. A failure fails the pass (set -e) —
# that is the loud surfacing channel for e.g. a pin mismatch.
bash provision/claude-home-sync.sh
