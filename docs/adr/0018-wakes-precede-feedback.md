# 0018. Wakes precede feedback in each pass

Date: 2026-11-13

## Context

Sharing admission logic could be mistaken for merging the two queues and changing which work receives scarce capacity first.

## Decision

run_pass calls `_resume_woken` before `_spawn_feedback`, with one generator iteration per call. Within each queue, tasks are oldest updated_at first, with repository-list order breaking ties. Do not merge the queues into one ordering as part of this shared policy.

## Consequences

Woken work retains precedence over pending PR feedback. Both queues use identical admission rules while retaining their separate candidate snapshots.
