#!/usr/bin/env bash
# Remove task worktrees that hold no recoverable work.
#
# The dispatcher already tears a worktree down when its PR merges
# (workspace.remove_workspace, the ONE sanctioned deletion on the done
# path). This script is the safety net for the cases that path never
# reaches: tasks whose state file is gone so the dispatcher no longer
# knows they exist, and tasks stuck at stage=failed whose PR actually
# merged (a state-machine desync, not a real failure). Both leave a
# ~770 MB checkout behind forever.
#
# It deliberately does NOT trust dispatcher state. Git and GitHub are
# ground truth: if every commit is in origin/main, nothing is uncommitted
# or unpushed, and the issue is closed, then no work can be lost — whatever
# the state file claims. What the module rule in workspace.py protects
# (crashed-task autopsy) is preserved instead by snapshotting .agent/
# into <state>/autopsy/ before deleting.
#
# Two modes:
#   sweep-worktrees.sh <task-id|path>   evaluate one worktree, remove if safe
#   sweep-worktrees.sh --sweep          remove every stale, safe worktree
# `--dry-run` reports without touching anything.
#
# Exit: 0 removed / nothing to do, 3 single target refused, 1 error.
set -euo pipefail

REPO_DIR="${AGENT_OPS_REPO:-$HOME/agent-ops}"
STATE_DIR="${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}"
TARGETS="${AGENT_OPS_TARGETS:-$STATE_DIR/targets.yaml}"
STALE_DAYS="${AGENT_OPS_WORKTREE_STALE_DAYS:-7}"
GH="${AGENT_OPS_GH:-gh}"
# The herdr binary is a per-user install in ~/.local/bin, which the user
# manager's PATH does not include (same rule as claude).
HERDR="${AGENT_OPS_HERDR:-$HOME/.local/bin/herdr}"
PODMAN="${AGENT_OPS_PODMAN:-podman}"

PY_BIN="${AGENT_OPS_PYTHON:-}"
if [ -z "$PY_BIN" ]; then
  if [ -x "$REPO_DIR/.venv/bin/python" ]; then
    PY_BIN="$REPO_DIR/.venv/bin/python"
  else
    PY_BIN=python3
  fi
fi

SWEEP=0
DRY=0
ONE=""
for arg in "$@"; do
  case "$arg" in
    --sweep) SWEEP=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 1 ;;
    *) ONE="$arg" ;;
  esac
done
if [ "$SWEEP" -eq 0 ] && [ -z "$ONE" ]; then
  echo "usage: $(basename "$0") [--dry-run] (--sweep | <task-id|path>)" >&2
  exit 1
fi

# --- lock -------------------------------------------------------------------
# Shares <state>/convergence.lock with the dispatcher pass and update.sh: a
# sweep must never run while a pass is mid-`git worktree add`, or we would
# evaluate a half-provisioned checkout and delete it as "no commits".
# Same fd-9 trick as update.sh — macOS has no flock(1), and the lock lives
# on the shared open file description so it survives the python child.
mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/convergence.lock"
if ! python3 - <<'PY'
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
then
  echo "convergence lock held by another pass; skipping sweep" >&2
  exit 1
fi

# --- helpers ----------------------------------------------------------------
_targets() {
  "$PY_BIN" - "$TARGETS" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1])) or {}
for t in doc.get("targets") or []:
    print("\t".join([t.get("name", ""), t.get("repo", ""),
                     t.get("clone_path", ""), t.get("worktrees_path", "")]))
PY
}

_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }

# The target a worktree was actually PROVISIONED under (workspace.py writes
# .agent/task.json at create_workspace time), not the target's current
# config name — a target renamed after provisioning must not orphan the
# session/state-file names still in use by a task that predates the rename.
# Empty output means "unknown": either the worktree predates Task 3's
# target field, or task.json itself, or is unreadable. Callers must treat
# unknown as "match by anchored issue number instead of exact name".
_task_target() {
  local tj="$1/.agent/task.json"
  [ -f "$tj" ] || return 0
  "$PY_BIN" - "$tj" 2>/dev/null <<'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(0)
t = d.get("target")
if t:
    print(t)
PY
}

_default_branch() {
  local clone=$1 ref
  ref=$(git -C "$clone" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
  if [ -n "$ref" ]; then echo "$ref"; return; fi
  for cand in origin/main origin/master; do
    if git -C "$clone" show-ref --quiet --verify "refs/remotes/$cand"; then
      echo "$cand"; return
    fi
  done
  echo origin/main
}

# One label per line for every herdr tab on the server: task-<target>-<n>
# for sessions, `triage` for the sweep. herdr is the session layer (spec
# 2026-09-03) and a tab exists for exactly as long as the dispatcher
# considers the session alive, so this list IS the set of live sessions.
# Empty when the server or the binary is absent — the podman check still
# covers a live container in that case.
_herdr_tabs() {
  "$HERDR" tab list 2>/dev/null | "$PY_BIN" -c '
import json, sys
try:
    tabs = json.load(sys.stdin)["result"]["tabs"]
except Exception:
    sys.exit(0)
for t in tabs:
    print(t.get("label", ""))
' 2>/dev/null || true
}

# Event schema is owned by dispatcher/eventlog.py; kept in sync by hand so
# this script stays dependency-free (it runs before/without the venv).
_log_event() {
  local issue=$1 target=$2 detail=$3
  ISSUE="$issue" TARGET="$target" DETAIL="$detail" \
  EVENTS="$STATE_DIR/events.jsonl" python3 - <<'PY' || true
import json, os
from datetime import datetime, timezone
line = json.dumps({
    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "event": "worktree_swept", "target": os.environ["TARGET"],
    "issue": int(os.environ["ISSUE"]), "stage": "", "model": "",
    "actor": "sweeper", "detail": os.environ["DETAIL"]})
with open(os.environ["EVENTS"], "a") as fh:
    fh.write(line + "\n")
PY
}

# Every reason a worktree is NOT safe to delete. Prints the reason and
# returns 1; silence and 0 mean "nothing here can be lost".
# Ordered cheapest-and-most-decisive first so the sweep makes as few gh
# calls as possible.
evaluate() {
  local clone=$1 repo=$2 wt=$3 issue=$4 check_age=$5
  local branch="agent/task-$issue" main dirty ahead unpushed newest age state
  # "" means unknown (pre-Task-3 worktree, or no task.json at all) — every
  # check below falls back to an anchored task-<anything>-<issue> match
  # when target is unknown, rather than skipping the new-style name outright.
  local target
  target=$(_task_target "$wt")

  if ! git -C "$clone" worktree list --porcelain \
       | grep -Fxq "worktree $(realpath "$wt")"; then
    echo "not registered as a worktree of $clone"; return 1
  fi

  local sessions containers
  sessions=$(_herdr_tabs)
  containers=$("$PODMAN" ps --format '{{.Names}}' 2>/dev/null || true)

  if echo "$sessions" | grep -Fxq "task-$issue"; then
    echo "session task-$issue is live"; return 1
  fi
  if [ -n "$target" ]; then
    if echo "$sessions" | grep -Fxq "task-$target-$issue"; then
      echo "session task-$target-$issue is live"; return 1
    fi
  elif echo "$sessions" | grep -Eq "^task-.+-$issue\$"; then
    echo "session task-<target>-$issue is live"; return 1
  fi

  if echo "$containers" | grep -Fxq "task-$issue"; then
    echo "container task-$issue is live"; return 1
  fi
  if [ -n "$target" ]; then
    if echo "$containers" | grep -Fxq "task-$target-$issue"; then
      echo "container task-$target-$issue is live"; return 1
    fi
  elif echo "$containers" | grep -Eq "^task-.+-$issue\$"; then
    echo "container task-<target>-$issue is live"; return 1
  fi

  if [ "$check_age" -eq 1 ]; then
    # Both signals must be old: a worktree can be committed to without its
    # top dir changing, and provisioned without any commit of its own.
    newest=$(git -C "$wt" log -1 --format=%ct 2>/dev/null || echo 0)
    state=$(_mtime "$wt")
    [ "$state" -gt "$newest" ] && newest=$state
    age=$(( ( $(date +%s) - newest ) / 86400 ))
    if [ "$age" -lt "$STALE_DAYS" ]; then
      echo "not stale (last activity ${age}d ago, threshold ${STALE_DAYS}d)"
      return 1
    fi
  fi

  # .my-skills.json is rewritten by claude-home-sync in every worktree on
  # the box. It is provisioning drift, never work — ignoring it is the
  # difference between this script finding candidates and finding none.
  dirty=$(git -C "$wt" status --porcelain --untracked-files=no \
          | grep -v '\.my-skills\.json$' || true)
  if [ -n "$dirty" ]; then
    echo "uncommitted changes ($(echo "$dirty" | wc -l) file(s))"; return 1
  fi

  if git -C "$clone" show-ref --quiet --verify "refs/remotes/origin/$branch"; then
    unpushed=$(git -C "$wt" rev-list --count "origin/$branch..HEAD" 2>/dev/null || echo 0)
    if [ "$unpushed" -gt 0 ]; then
      echo "$unpushed unpushed commit(s)"; return 1
    fi
  fi

  main=$(_default_branch "$clone")
  ahead=$(git -C "$clone" rev-list --count "$main..$branch" 2>/dev/null || echo 1)
  if [ "$ahead" -gt 0 ]; then
    echo "$ahead commit(s) not in $main"; return 1
  fi

  # Fail closed: an unreachable GitHub means we do not know, and "do not
  # know" must never delete.
  local st
  st=$("$GH" issue view "$issue" --repo "$repo" --json state --jq .state 2>/dev/null || true)
  if [ "$st" != "CLOSED" ]; then
    echo "issue #$issue is ${st:-unreadable}, not closed"; return 1
  fi
  return 0
}

remove_worktree() {
  local clone=$1 name=$2 wt=$3 issue=$4
  local branch="agent/task-$issue" size target
  # Resolved before anything below touches/removes .agent/ — same source
  # evaluate() used, so a target rename mid-life can't make the two checks
  # disagree about which state file/session name belongs to this task.
  target=$(_task_target "$wt")
  size=$(du -sh "$wt" 2>/dev/null | cut -f1 || echo "?")

  if [ "$DRY" -eq 1 ]; then
    echo "DRY-RUN would remove task-$issue ($size)"
    return 0
  fi

  # Autopsy first: .agent/ is the only irreplaceable thing in here (plan,
  # stage signal, model log) and it is a few KB against ~770 MB.
  if [ -d "$wt/.agent" ]; then
    mkdir -p "$STATE_DIR/autopsy"
    rm -rf "$STATE_DIR/autopsy/task-$issue-agent"
    cp -a "$wt/.agent" "$STATE_DIR/autopsy/task-$issue-agent"
  fi

  if ! git -C "$clone" worktree remove --force "$wt" 2>/dev/null; then
    rm -rf "$wt"
    git -C "$clone" worktree prune || true
  fi
  # -D not -d: the merge is already proven against origin/main above, and
  # -d compares against local HEAD, which on this box lags origin by weeks.
  git -C "$clone" branch -D "$branch" 2>/dev/null || true
  if git -C "$clone" ls-remote --heads origin "$branch" 2>/dev/null | grep -q .; then
    git -C "$clone" push origin --delete "$branch" >/dev/null 2>&1 || true
  fi
  rm -f "$STATE_DIR/task-$issue.json"
  if [ -n "$target" ]; then
    rm -f "$STATE_DIR/task-$target-$issue.json"
  else
    # Unknown target (task.json predates the target field, or is missing
    # entirely): fall back to $name, the config target this worktree is
    # PHYSICALLY under — it lives inside $name's worktrees_path, so that is
    # the only target this deletion may ever touch. NOT a cross-target
    # wildcard: a glob here could delete a live, in-flight task's state
    # file in an unrelated target that happens to share this issue number
    # (evaluate()'s anchored-regex fallback is safe to guess broadly
    # because a false positive there only causes a SKIP; this is a
    # deletion, so it must never guess past what we structurally know).
    rm -f "$STATE_DIR/task-$name-$issue.json"
  fi

  _log_event "$issue" "$name" "swept worktree $wt ($size)"
  echo "REMOVED task-$issue ($size freed)"
}

handle() {
  local clone=$1 repo=$2 name=$3 wt=$4 issue=$5 check_age=$6 reason
  if reason=$(evaluate "$clone" "$repo" "$wt" "$issue" "$check_age"); then
    remove_worktree "$clone" "$name" "$wt" "$issue"
    return 0
  fi
  echo "SKIP task-$issue: $reason"
  return 3
}

# --- main -------------------------------------------------------------------
[ -f "$TARGETS" ] || { echo "no targets file at $TARGETS" >&2; exit 1; }

rc=0
found=0
while IFS=$'\t' read -r name repo clone worktrees; do
  [ -n "$clone" ] && [ -d "$clone" ] || continue
  git -C "$clone" fetch origin --prune --quiet 2>/dev/null || true

  for wt in "$worktrees"/task-*; do
    [ -d "$wt" ] || continue
    issue=$(basename "$wt"); issue=${issue#task-}
    case "$issue" in ''|*[!0-9]*) continue ;; esac

    if [ "$SWEEP" -eq 1 ]; then
      found=1
      handle "$clone" "$repo" "$name" "$wt" "$issue" 1 || true
    else
      # Single mode answers "is this one safe to remove", which is a
      # different question from "has it been abandoned" — no age gate.
      case "$ONE" in
        "$issue"|"task-$issue") ;;
        *) [ "$(realpath "$ONE" 2>/dev/null || echo)" = "$(realpath "$wt")" ] || continue ;;
      esac
      found=1
      handle "$clone" "$repo" "$name" "$wt" "$issue" 0 || rc=3
    fi
  done
done < <(_targets)

if [ "$found" -eq 0 ] && [ "$SWEEP" -eq 0 ]; then
  echo "no worktree matching '$ONE'" >&2
  exit 1
fi
exit $rc
