# 0011. FailureList busy state uses task identity

Date: 2026-11-10

## Context

Issue numbers are scoped to repositories. A shared busy boolean disables unrelated rows, and an issue-only key conflates tasks in different repositories.

## Decision

FailureList accepts in-flight keys as a ReadonlySet<string>, defaulting to empty. Keys use `${repository}#${task_issue}`, matching the existing per-task errors convention. Disable a Retry button only when its complete task key is in the set.

## Consequences

Callers provide the whole pending set rather than a latest-request flag. Tasks with the same issue number in different repositories remain independently retryable.
