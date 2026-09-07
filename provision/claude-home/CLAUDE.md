# Box session conventions (claude-home seed — ADR 0003)

You are a session on the agent-ops box, working exactly one stage of one
task (spec → plan → implement, one ticket per session → review →
address-review) inside a task worktree. Your only inputs are the stage
prompt, the artifacts under `.agent/` and `docs/specs/`, the issue thread
and the repo checkout. You have no memory of earlier sessions and nobody is
watching the chat: never wait for an answer in chat — signal through
`.agent/stage.json` exactly as the stage prompt says.

## Git and PRs

- NEVER add co-author lines, "Generated with", or any agent attribution to
  commit messages or PR bodies.
- Conventional Commits: feat:, fix:, docs:, test:, refactor:, chore:.
- Commit small and often; every commit leaves the tree green.
- Push only the task branch. Never push to main or master. The only forced
  push allowed is `git push --force-with-lease origin <task branch>` alone
  on its line after a rebase onto origin/main; a guardrail hook blocks every
  other force push.

## Stage discipline

- TDD for every feature and bugfix: failing test first, minimal code to
  green, then commit. No implementation before a failing test.
- The spec's testing decisions name the seams; do not renegotiate them
  mid-run.
- Run the repository gate command the prompt names after every ticket;
  never open or update a PR that has not passed it.
- Specs are committed to the task branch. Tickets, questionnaires,
  prototypes and every other stage artifact live under `.agent/` and are
  never committed (nothing under `.agent/` is).
- Bounded loops: before every fix round of a capped loop, record the round
  in `.agent/stage.json` as the prompt describes. Exceeding the cap parks
  the task for the operator; do not fight the cap.
- If the spec or a ticket is ambiguous, contradicts the code, or needs a
  decision only the operator can make, signal `blocked` or
  `awaiting-answers` with a specific note instead of guessing.
- When the stage's deliverable is done, stop. Do not expand scope, refactor
  unrelated code, or start the next stage.
