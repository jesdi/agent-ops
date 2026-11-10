# 0008. Retry busy state comes from the mutation cache

Date: 2026-11-10

## Context

Several quarantined tasks can be retried concurrently. A single mutation observer follows only its latest call; a second local lifecycle store can drift from the mutation cache.

## Decision

FailuresPage derives its in-flight retry keys from React Query useMutationState, selecting pending retries from the mutation cache. Do not maintain a parallel set through onMutate/onSettled callbacks.

## Consequences

The cache owns request lifecycle state. Every concurrent request contributes to the derived set, and settling one request cannot enable another task’s pending retry.
