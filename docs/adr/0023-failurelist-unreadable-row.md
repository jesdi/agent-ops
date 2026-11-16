# 0023. FailureList renders unreadability explicitly

Date: 2026-11-16

## Context

A row of blank fingerprint, blocker and time values gives the operator no useful explanation for a blocked task.

## Decision

FailureList gives unreadable entries their own details branch: visible `{repository}#{task_issue}` and “quarantine record unreadable — still blocking”, without fingerprint, blocker text, blocker-state chip or relative time. Retry, pending state and inline error handling remain shared and keyed by `${repository}#${task_issue}` as in ADR 0011 (failurelist-busy-state-uses-task-identity).

## Consequences

Readable details retain their prior rendering. Unreadable rows offer the same retry interaction without presenting fabricated metadata.
