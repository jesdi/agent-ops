# 0008. Auth dark marker owns alert state

Date: 2026-11-10

## Context

The auth outage needs one persisted record that survives dispatcher passes.

## Decision

- **The marker gains one field, `last_alert`** (ISO-8601 UTC, written by `_now()`), beside
  `since` and `alerted` — the issue's own proposal; the marker stays the only state, so no
  second file to keep in step with it.

## Consequences

The marker remains the only incident state; no parallel alert state needs synchronization.
