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
    #
    # Template units (`foo@.service`) are the one exception, and only in this
    # RESTART loop — they are still copied above and picked up by the
    # daemon-reload. systemd rejects `try-restart foo@.service` ("missing the
    # instance name") with a non-zero exit, which under `set -euo pipefail`
    # would abort the pass. The glob sorts agent-ops-alert@.service first, so
    # the deploy pass would die before the keepalive timer restart, the
    # credential convergence and claude-home sync — silently, since the
    # updater unit has no OnFailure of its own.
    case "$unit" in *@.service) continue ;; esac
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
  # A temp left behind by a failed install is harmless — the next pass
  # overwrites it — and `rm -f` clears it on the way out either way.
  install -m 600 "$HOST_CREDS" "$CH_CREDS.tmp" \
    && mv -f "$CH_CREDS.tmp" "$CH_CREDS"
  rm -f "$CH_CREDS.tmp"
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
