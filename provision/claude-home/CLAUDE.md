# Box session conventions (claude-home seed — ADR 0003)

You are a session on the agent-ops box, working exactly one stage of one
task (spec → plan → implement) inside a task worktree. Your only inputs are
the stage prompt, the previous stage's artifact, and the repo checkout.

## Git and PRs

- NEVER add co-author lines, "Generated with", or any agent attribution to
  commit messages or PR bodies.
- Conventional Commits: feat:, fix:, docs:, test:, refactor:, chore:.
- Commit small and often; every commit leaves the tree green.
- Push only the task branch. Never push to main or master; never force-push
  (a guardrail hook also enforces this).

## Stage discipline

- TDD for every feature and bugfix: failing test first, minimal code to
  green, then commit. No implementation before a failing test.
- If the spec or plan is ambiguous or incomplete, record the gap explicitly
  in your output artifact instead of guessing.
- Specs are committed to the task branch; plans live at `.agent/plan.md`
  and are never committed (nothing under `.agent/` is).
- When the stage's deliverable is done, stop. Do not expand scope, refactor
  unrelated code, or start the next stage.
