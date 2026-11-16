# 0023. Fake API reset has a sequential contract

Date: 2026-11-16

## Context

The fake API serves serial tests with one shared process. Request bodies may complete asynchronously.

## Decision

Tests await mutation responses before resetting. Mutations whose request bodies finish after reset are outside this serial contract. Add no locking or cancellation protocol for overlapping requests.

## Consequences

Parallel workers or concurrent reset semantics require a separate design decision.
