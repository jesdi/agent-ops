# 0006. A task's base comes from its issue and is fixed at claim

Date: 2026-10-29

## Context

agents-wayfinder hands off increments of an all-at-once cut-over as build
issues whose PRs must target a `feat/<name>` integration branch, not main.
Every task used to branch from, review against, rebase onto and open its PR
into `main`.

## Decision

- The only source of a task's base is the first `Base: <branch>` line of its
  issue body, read at claim. No label, board field, project or repository
  setting, and no console control. Without the line the base is `main`; the
  dispatcher does not read the repository's default branch.
- The base is stored on the task once, at claim, and never written again.
  Editing the line later has no effect on a claimed task.
- Any valid branch on origin may be a base; there is no `feat/*` rule. A
  base that is not on origin, or a line that names no valid branch, is not a
  failure: the issue waits in Ready and the operator is pinged once per
  issue and branch.
- The dispatcher never creates, rebases, merges or deletes an integration
  branch. A base rewritten under a task in flight goes through the existing
  conflict path. The guard against merging the final PR while increments
  still target the base lives where that merge happens, in babysit-pr
  (jesdi/general-skills#153).
- The box closes the issue itself when it finishes a merged task, because
  GitHub closes an issue from `Closes #N` only on a merge into the default
  branch.

## Consequences

- "No base line" and `Base: main` cannot be told apart later; nothing needs
  to.
- A task saved before the base existed loads with `main`, with no migration.
- Each claim reads the issue body: one `gh` call per candidate about to be
  claimed.
