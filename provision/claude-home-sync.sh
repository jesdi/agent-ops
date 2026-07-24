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
import json, sys
seed = json.load(open(sys.argv[1]))
out = dict(seed)
out["enabledPlugins"] = {k: True for k in seed.get("enabledPlugins", {})}
with open(sys.argv[2], "w") as f:
    json.dump(out, f, indent=2)
    f.write("\n")
PY

cp "$SEED/CLAUDE.md" "$CLAUDE_HOME/CLAUDE.md"
rsync -a --delete "$SEED/skills/" "$CLAUDE_HOME/skills/"
rsync -a --delete "$SEED/hooks/" "$CLAUDE_HOME/hooks/"
