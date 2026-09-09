#!/usr/bin/env bash
# Vendor the pinned process-skill set into the claude-home seed (ADR 0003,
# spec 2026-09-07-agentic-orchestration). Sources: the published
# @jesdi/skills npm package (the four forks + deep-quality-review) and a
# pinned mattpocock/skills git ref (the unmodified upstream set). Idempotent
# and destructive inside the destination: the resulting git diff IS the
# review. Test seams: AGENT_OPS_SKILLS_{PINS,DEST,NPM_TARBALL,GH_TARBALL}.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PINS="${AGENT_OPS_SKILLS_PINS:-$REPO_DIR/provision/skills-pins.json}"
DEST="${AGENT_OPS_SKILLS_DEST:-$REPO_DIR/provision/claude-home/skills}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

command -v jq >/dev/null || { echo "vendor-skills: jq is required" >&2; exit 1; }

pkg=$(jq -r .jesdi.package "$PINS"); ver=$(jq -r .jesdi.version "$PINS")
repo=$(jq -r .mattpocock.repo "$PINS"); ref=$(jq -r .mattpocock.ref "$PINS")

if [ -n "${AGENT_OPS_SKILLS_NPM_TARBALL:-}" ]; then
  cp "$AGENT_OPS_SKILLS_NPM_TARBALL" "$TMP/npm.tgz"
else
  (cd "$TMP" && npm pack "$pkg@$ver" --silent >/dev/null && mv ./*.tgz npm.tgz)
fi
if [ -n "${AGENT_OPS_SKILLS_GH_TARBALL:-}" ]; then
  cp "$AGENT_OPS_SKILLS_GH_TARBALL" "$TMP/gh.tgz"
else
  curl -fsSL "https://codeload.github.com/$repo/tar.gz/$ref" -o "$TMP/gh.tgz"
fi
mkdir -p "$TMP/npm" "$TMP/gh" "$TMP/stage"
tar -xzf "$TMP/npm.tgz" -C "$TMP/npm" --strip-components=1
tar -xzf "$TMP/gh.tgz" -C "$TMP/gh" --strip-components=1

record='{}'
copy_skill() {  # name, extracted root, source label
  local name=$1 root=$2 label=$3 dir
  # @jesdi/skills is skills/<name>; mattpocock is skills/<group>/<name>.
  dir=$(find "$root/skills" -mindepth 1 -maxdepth 2 -type d -name "$name" | head -1)
  if [ -z "$dir" ] || [ ! -f "$dir/SKILL.md" ]; then
    echo "vendor-skills: skill '$name' not found under $label" >&2
    exit 1
  fi
  cp -R "$dir" "$TMP/stage/$name"
  record=$(jq --arg n "$name" --arg s "$label" '. + {($n): $s}' <<<"$record")
}

for n in $(jq -r '.jesdi.skills[]' "$PINS"); do copy_skill "$n" "$TMP/npm" "$pkg@$ver"; done
for n in $(jq -r '.mattpocock.skills[]' "$PINS"); do copy_skill "$n" "$TMP/gh" "$repo@$ref"; done
jq -n --argjson s "$record" '{skills: $s}' > "$TMP/stage/VENDORED.json"

mkdir -p "$DEST"
rsync -a --delete --exclude .gitkeep "$TMP/stage/" "$DEST/"
echo "vendor-skills: $(jq '.skills | length' "$TMP/stage/VENDORED.json") skills into $DEST"
