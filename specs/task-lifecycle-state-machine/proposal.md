# Task lifecycle as one explicit state machine

## Why

`dispatcher/machine.py` decides only what happens when a session reports. Every other trigger
(operator intents, wakes and resumes, parks, PR polls, crashes, claims, Telegram) mutates
`TaskState` directly in `dispatcher/main.py`, each behind its own guard. The real state is spread
over `stage` × `park` × `operator_request` × `feedback_pending` × `hold_for_attach` ×
`crashed_stage`, and no single place says which combinations are valid or which events each one
accepts. jesdi/portfolio_eval#363 showed the cost: nobody had decided what "resume a FAILED task"
means, so the resume fell into a generic `if not _is_parked: skip` and was dropped silently; the
FAILED state file then blocked a re-claim forever, and replies stayed queued forever. Because
`FAILED` overwrites `stage`, "failed during implement, ticket 2" could not even be represented.
PR #131 (`crashed_stage`) is the stopgap this work replaces.

## For whom

- **Operator**: sees where a task is and why, gets a loud answer when an action doesn't apply,
  and can always resume or re-queue a failed task.
- **Dispatcher maintainers**: one pure table to read and test instead of ~30 executors that each
  write state.

## Goal

Every task transition, from every trigger, goes through one pure, declarative
`(status, event) → transition` table in `dispatcher/lifecycle.py`, where `stage` (pipeline
position) and `status` (what the task is doing there) are separate fields, and a CI test fails on
any status × event pair the table does not declare.

## Non-goals

- **Liveness "unknown ≠ dead".** `Sessions.is_alive` still reads a herdr outage as a dead session.
  The event enum is shaped so a later `liveness-unknown` event is one event plus its rows.
- **Failure-fingerprint dedupe** of a repeat crash after a resume.
- **A graph/DAG visualization in the console.** This spec only ships the data it needs
  (`graph()`, `next_transitions(task)`, uniform `transition` events); the UI is a follow-up issue.
- **Inbound Telegram.** `/attach`, replies, plain text, `/status`, `/queue`, `/boost`, `/next`
  and login-code replies are removed, not ported. Outbound notifications and the morning digest
  stay.
- **Changing the usage/admission gate.** Admission stays outside the table as an input to events.
- **Changing loop-round accounting.** `loops.py` keeps ownership; the table calls it.
- **Reverse compatibility of task files.** New files are never read by old code (dispatcher and
  web deploy together).
- **A one-shot migration script.** Migration is lazy, in `state._read`.
