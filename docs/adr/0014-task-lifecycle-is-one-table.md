# 0014. The task lifecycle is one table over stage and status

Date: 2026-11-11

## Context

A task's real state was spread over `stage`, `park`, `operator_request`,
`feedback_pending`, `hold_for_attach` and `crashed_stage`, and every trigger
(session signals, operator intents, wakes, PR polls, crashes, claims) wrote
them behind its own guard. No place said which combinations are valid or which
events each accepts; a Resume of a failed task was dropped without a word
(jesdi/portfolio_eval#363).

## Decision

- `stage` is the pipeline position only. `status` says what the task does
  there: `running`, `held`, `parked`, `waking`, `failed`, `canceled`, `done`.
  A task that fails, is canceled or is done keeps its stage.
- `dispatcher/lifecycle.py` holds one pure `decide(task, event)` over
  declarative rows. It reads no file, clock, session or network: the executor
  puts the facts in the event.
- Every status kind × event pair is a declared `Transition`, `Ignore` or
  `Reject`. A test fails on an undeclared pair.
- Only the transition executor saves a changed `stage` or `status`. A test
  enforces it.
- A `Reject` is loud: an event in the dispatcher, a 409 in the console.
- `slot` follows `status`: a task holds a slot and a capacity unit exactly
  while it is `running` or `held`.

## Consequences

- A new trigger is a new event type plus its rows, not a new guard in
  `main.py`.
- A new stored fact about a task is a field only when it is not derivable
  from `stage` and `status`.
- Admission and loop-round accounting stay outside the table, as inputs.
