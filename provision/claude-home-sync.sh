#!/usr/bin/env bash
# Converge claude-home from the versioned seed (ADR 0003 §2–3). The updater
# has FULL authority inside seed-managed paths — settings.json, CLAUDE.md,
# skills/, hooks/ — including delete-propagation. Machine state is never
# touched: .credentials.json, projects/ transcripts (park/resume's
# --continue depends on them), the plugins/ cache, and anything else living
# in claude-home. Invoked by update.sh every convergence pass and by
# bootstrap.sh once at install time.
set -euo pipefail

REPO_DIR="${AGENT_OPS_REPO:-$HOME/agent-ops}"
STATE_DIR="${AGENT_OPS_STATE_DIR:-$HOME/agent-ops-state}"
CLAUDE_HOME="${AGENT_OPS_CLAUDE_HOME:-$STATE_DIR/claude-home}"
SEED="$REPO_DIR/provision/claude-home"

mkdir -p "$CLAUDE_HOME"

# settings.json: the seed's enabledPlugins values declare latest (true) or
# a pinned version ("1.2.3") — that is a seed-only convention consumed by
# the plugin step; the runtime schema wants booleans, so normalize.
python3 - "$SEED/settings.json" "$CLAUDE_HOME/settings.json" <<'PY'
import json, os, sys
seed = json.load(open(sys.argv[1]))
out = dict(seed)
out["enabledPlugins"] = {k: True for k in seed.get("enabledPlugins", {})}
tmp = sys.argv[2] + ".tmp"
with open(tmp, "w") as f:
    json.dump(out, f, indent=2)
    f.write("\n")
os.replace(tmp, sys.argv[2])
PY

cp "$SEED/CLAUDE.md" "$CLAUDE_HOME/CLAUDE.md.tmp"
mv "$CLAUDE_HOME/CLAUDE.md.tmp" "$CLAUDE_HOME/CLAUDE.md"
rsync -a --delete "$SEED/skills/" "$CLAUDE_HOME/skills/"
rsync -a --delete "$SEED/hooks/" "$CLAUDE_HOME/hooks/"

# --- plugins: machine-managed, converged via the claude CLI ------------------
# Declared set = seed enabledPlugins (true = track latest, "x.y.z" = pin).
# Missing → install; undeclared → uninstall; latest-tracking plugins are
# only `plugin update`d when the declared set changed since the last pass
# (stamp) — we do not chase upstream releases every minute. The verify step
# at the end is the enforcement: declared-but-missing or off-pin fails the
# pass loudly (pinned installs use the `id@version` argument, whose CLI
# support is unverified — if it is ignored, verify catches it; the ADR's
# fallback is then vendoring skills into the seed).
CLAUDE="${AGENT_OPS_CLAUDE:-claude}"
export CLAUDE_CONFIG_DIR="$CLAUDE_HOME"
stamp_file="$STATE_DIR/claude-home-plugins.stamp"

installed_json="$("$CLAUDE" plugin list --json)"
actions=$(AGENT_OPS_INSTALLED="$installed_json" \
python3 - "$SEED/settings.json" "$stamp_file" <<'PY'
import hashlib, json, os, sys
declared = json.load(open(sys.argv[1])).get("enabledPlugins", {})
digest = hashlib.sha256(
    json.dumps(declared, sort_keys=True).encode()).hexdigest()
try:
    stale = open(sys.argv[2]).read().strip() != digest
except FileNotFoundError:
    stale = True
installed = {p["id"]: p["version"]
             for p in json.loads(os.environ["AGENT_OPS_INSTALLED"])}
for pid, want in declared.items():
    if pid not in installed:
        print(f"install\t{pid}" if want is True else f"install\t{pid}@{want}")
    elif want is not True and installed[pid] != want:
        print(f"install\t{pid}@{want}")
    elif want is True and stale:
        print(f"update\t{pid}")
for pid in installed:
    if pid not in declared:
        print(f"uninstall\t{pid}")
print(f"stamp\t{digest}")
PY
)

new_stamp=""
while IFS=$'\t' read -r verb arg; do
  case "$verb" in
    install)   "$CLAUDE" plugin install "$arg" ;;
    update)    "$CLAUDE" plugin update "$arg" ;;
    uninstall) "$CLAUDE" plugin uninstall "$arg" ;;
    stamp)     new_stamp="$arg" ;;
  esac
done <<< "$actions"

installed_json="$("$CLAUDE" plugin list --json)"
AGENT_OPS_INSTALLED="$installed_json" \
python3 - "$SEED/settings.json" <<'PY'
import json, os, sys
declared = json.load(open(sys.argv[1])).get("enabledPlugins", {})
installed = {p["id"]: p["version"]
             for p in json.loads(os.environ["AGENT_OPS_INSTALLED"])}
bad = [f"{pid}: want {want if want is not True else 'latest'}, "
       f"have {installed.get(pid, 'nothing')}"
       for pid, want in declared.items()
       if pid not in installed
       or (want is not True and installed[pid] != want)]
if bad:
    sys.exit("claude-home plugin convergence failed:\n  " + "\n  ".join(bad))
PY

echo "$new_stamp" > "$stamp_file"
