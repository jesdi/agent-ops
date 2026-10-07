# 0003. Implement is one session; the dispatcher keeps no ticket state

Date: 2026-10-07

## Context

The dispatcher used to start one session per ticket and to keep a ticket
cursor on the task. The `implement-spec` skill has its own task graph, test
author, per-ticket review and fix-loop limit, and it runs several tickets at
once.

## Decision

- The implement stage is one session that runs `implement-spec` over the
  whole ticket set, on the task branch that already exists.
- The dispatcher does not know which ticket is in work. It shows the
  progress note the session last reported.
- The usage gate is asked once, when the session starts.
- No round an implement session reports is counted by the dispatcher's loop
  caps. The skill's own limit rules.
- The session keeps its ledger and notes in `.agent/`, never committed, and
  its ticket worktrees under `.agent/worktrees/`. A session that finds a
  ledger continues from it.
- The implement session opens no pull request. The review stage alone does.
- A dead implement session fails the task as any dead session does; there is
  no automatic restart.

## Consequences

- A park of the implement session ends its container and every subagent in
  work. The work survives as commits and the ledger.
- A feature that needs per-ticket state in the dispatcher (a per-ticket
  usage check, a per-ticket notification) contradicts this decision and
  needs a new one.
- The stage prompt binds the skill with a small number of overrides; the
  method lives in the skill file, which the prompt tells the session to read.
