#!/bin/bash
# Box-variant git guardrail (ADR 0003 §1), PreToolUse hook for Bash.
# Sessions MUST push task branches and open PRs, so plain `git push` is
# allowed. Blocked: force pushes, any push to main/master (incl. refspecs),
# and history-destroying commands. Branch protection on target repos is the
# primary enforcement; this hook is defense in depth. Unlike the mac hook
# there is deliberately NO override token: sessions are unattended.

# Fail closed: without jq we cannot inspect the command, so block everything.
command -v jq >/dev/null || {
  echo "BLOCKED: jq missing — guardrail cannot inspect the command." >&2
  exit 2
}

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

block() {
  echo "BLOCKED: '$COMMAND' matches dangerous pattern '$1'. The box guardrail prevents this." >&2
  exit 2
}

if echo "$COMMAND" | grep -qE 'git[[:space:]]+push'; then
  echo "$COMMAND" | grep -qE '(--force|(^|[[:space:]])-f([[:space:]]|$))' \
    && block "force push"
  # +refspec (e.g. `git push origin +feature:branch`) is also a force push.
  echo "$COMMAND" | grep -qE '(^|[[:space:]])\+[^[:space:]]*:' \
    && block "force push via +refspec"
  echo "$COMMAND" | grep -qE '[[:space:]:](main|master)([[:space:]]|$)' \
    && block "push to main/master"
fi

DANGEROUS_PATTERNS=(
  "git reset --hard"
  "git clean -fd"
  "git clean -f"
  "git branch -D"
  "git checkout \."
  "git restore \."
  # Bare pattern intentionally catches invocations the git-prefixed pattern
  # misses: e.g. `git -C <dir> reset --hard`, `command git reset --hard`.
  "reset --hard"
)
for pattern in "${DANGEROUS_PATTERNS[@]}"; do
  echo "$COMMAND" | grep -qE "$pattern" && block "$pattern"
done

exit 0
