# 0001. The dispatcher alone decides the plan review gate

Date: 2026-10-07

## Context

A task waits for the operator once before a pull request exists: at the plan
review gate, after the spec, the design and the tickets are written. A track
can be marked gate-free (`plan_review: false`). The session that writes the
plan is a model, and it is the party whose work the gate reviews.

## Decision

- The plan session always reports "ready for review". It is never told that a
  gate can be skipped, and no signal it writes can skip it.
- The dispatcher skips the gate only when all of these hold: the task's track
  is gate-free; no spec or plan session of the task raised a questionnaire
  (`asked`); the ready report's `open_questions` is the integer 0 and equals
  the count of the summary's open-questions section; the task never waited at
  the gate before (`gated`).
- Every input to that decision fails closed. A missing, malformed, unreadable
  or ambiguous value means "the gate applies".
- A plan `done` is accepted only from the gate. The ticket set is checked on
  every path into implement.
- The armed approval request is bound to a fingerprint of what the operator
  is asked to approve. A changed plan is a new review round.
- Rounds at the gate that no operator action preceded are capped
  (`unattended_rounds`); only an operator action resets the cap.

## Consequences

- A change that adds a way to reach implement must go through the same ticket
  check and must not read a session-written value as permissive.
- A new condition for the skip belongs in `machine._skips_gate`, not in a
  prompt.
- The dispatcher cannot prove that an operator approved; it enforces only
  that `done` comes from the gate. "An answer is never an approval" is a
  prompt rule. A stronger guarantee needs an approval that does not pass
  through the session.
- A miscount or a strict-format miss on a gate-free track costs one review,
  never a skipped one.

## Amendment (2026-10-07)

The review page replaces the summary; the dispatcher counts the page's
question blocks. The skip condition on `open_questions` now reads: the
integer 0, equal to the number of `data-q` question blocks of
`.agent/review.html` (`artifacts.count_open_questions`). A block whose id is
not a question id makes the count unknown, so the gate applies.
