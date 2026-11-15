# 0008. Usage cache writes are best effort at the provider fetch seam

Date: 2026-11-13

## Context

The web console, dispatcher pass and triage obtain provider usage through
`fetch_provider`. A provider can return a valid reading while writing its
`usage/<provider>.json` cache fails. Propagating that write error turns a
healthy reading into a failed request or pass.

## Decision

- `fetch_provider` owns the cache-write guard; callers do not add separate
  cache-write handling.
- Catch `OSError` from the write, but let programming errors such as a
  serialization `TypeError` surface.
- Log one warning naming the provider when the write fails, and return the
  fresh reading unchanged rather than converting it to `unavailable`.
- Do not keep provider readings in an in-memory fallback cache. A failed
  write should make the next fetch call the adapter again.

## Consequences

- A cache-write failure costs cache availability and extra adapter calls while
  storage remains broken. The warning is the operator signal.
- The cache JSON format is unchanged.
- When an older cache cannot be removed after a failed refresh, the current
  process skips it, but another process can still serve it. A failed writer
  can also remove a concurrent writer's successful cache refresh. These
  accepted limitations require a shared invalidation rule or coordination
  before the cache can guarantee cross-process freshness in those cases.
