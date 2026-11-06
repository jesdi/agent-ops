# Usage cache write failure degrades instead of 500ing — Design

## Decisions

- **Guard the write inside `fetch_provider`, not in its callers** — one place covers `Sources.usage` (board, `/api/usage`, socket) and `fetch_all` (pass, triage); callers stay untouched.
- **Catch `OSError` only** — matches the non-goal; a serialisation `TypeError` is a programming error and must still surface.
- **Log a warning naming the provider** — the cache is best-effort and the journal line is the only operator signal (non-goal: alerting). While the disk stays broken it logs once per fetch, which is already rate-limited by the adapter call.
- **Return the fresh reading unchanged on failure** — a good reading is never degraded to `unavailable` because of the cache.
- **No in-memory fallback cache** — a failed write caches nothing (requirement 4); the next call refetches. Costs extra adapter calls while the disk is broken.

## Data model

None. `usage/<provider>.json` keeps its shape.

## Identities

None.

## Seams

- `dispatcher.usage_providers.fetch_provider` — existing; unit scenarios (return value, log, refetch).
- `dispatcher.usage_providers.fetch_all` — existing; pass scenario.
- `GET /api/board` — existing; reproduction scenario (failing test already committed).
