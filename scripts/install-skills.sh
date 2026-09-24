#!/usr/bin/env bash
# Install the jesdi/general-skills skills listed in .my-skills.json.
# They live in .my-skills/ and are symlinked into .claude/skills/, both gitignored.
# The mattpocock skills (skills-lock.json, .agents/skills/) are committed and need nothing.
set -euo pipefail
cd "$(dirname "$0")/.."

# An upstream skills.sh copy of a forked skill (e.g. `npx skills add mattpocock/skills`
# picked to-tickets) blocks the install; drop it if git doesn't track it.
for name in $(node -p "Object.keys(require('./.my-skills.json').skills).join(' ')"); do
  link=".claude/skills/$name"
  if [[ -L "$link" && "$(readlink "$link")" == ../../.agents/skills/* ]] \
    && [[ -z "$(git ls-files ".agents/skills/$name")" ]]; then
    echo "removing upstream copy of $name (the jesdi fork replaces it)"
    rm "$link"
    rm -rf ".agents/skills/$name"
  fi
done

pnpm dlx @jesdi/skills-cli@latest sync
