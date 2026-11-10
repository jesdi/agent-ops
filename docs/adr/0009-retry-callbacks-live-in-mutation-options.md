# 0009. Retry callbacks live in mutation options

Date: 2026-11-10

## Context

Concurrent retries each need to clear or populate their own inline error slot. Per-call mutate callbacks follow only the latest observed call.

## Decision

Keep retry success and error handling in the useMutation options, where callbacks execute for every concurrent request. Do not move this handling into callbacks passed to mutate().

## Consequences

Each request updates its own error slot even when a later retry has started. Functional state updates retain errors for other tasks.
