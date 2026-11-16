# 0020. Fake API startup and reset share fresh seeds

Date: 2026-11-16

## Context

Partial reset handlers let serial browser tests inherit state from previous tests.

## Decision

Startup and POST /__control__/reset use the same fresh seed builders for columns, capacity and next claim, together with the ghost seed builder. Capacity starts with zero occupied slots. Copy seeded cards and allocate fresh collections on each reset.

## Consequences

New mutable fixture domains must join the canonical reset. No partial reset endpoint is retained.
