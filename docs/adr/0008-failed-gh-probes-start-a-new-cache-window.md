# 0008. Failed gh probes start a new cache window when they finish

Date: 2026-11-10

## Context

The console caches issue descriptions and project rank rows. During a `gh`
outage, a cache entry whose TTL had elapsed caused every request to start a
new subprocess. A `gh` call can take 120 seconds, longer than the short retry
windows.

## Decision

A failed probe refreshes the cache TTL clock when the call finishes. Its
previous payload and payload timestamps remain unchanged. A successful fetch
keeps the existing timestamp behavior. The cache clock measures retry
eligibility after a failure; it does not claim that the payload was refreshed.

## Consequences

- Requests during an outage re-probe once per failure window, including after
  a call that consumed more than an entire window before failing.
- The description's `fetched_at` and rank rows' `as_of` continue to identify
  the payload's earlier fetch, while the internal TTL clock can be newer.

