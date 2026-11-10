# 0010. Description failures use per-entry retry lifetimes

Date: 2026-11-10

## Context

Fetched issue descriptions have a 300-second TTL. Holding a cold failure for
that long would delay recovery; retrying on every request would start a new
`gh` subprocess during an outage.

## Decision

Each description cache entry stores its own `ttl`. Successful fetches and
stale refreshes use `DESCRIPTION_TTL_SECONDS` (300 seconds). Cold failures
use `DESCRIPTION_RETRY_SECONDS` (30 seconds), as approved at the plan gate.
The freshness check uses the entry's `ttl`.

## Consequences

- A recovered `gh` can replace a cold failure after 30 seconds.
- Failure retries remain bounded during an outage without a second cache.
- The retry durations are module constants and can be changed by a later
  decision without changing the cache entry shape.

