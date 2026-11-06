# Usage cache write failure degrades instead of 500ing — Spec

Fixes jesdi/agent-ops#80. The binding constraint of the console: every
failure degrades to an explicit state, never a 500. The fix belongs in the
one shared function every caller routes through, `fetch_provider` in
`dispatcher/usage_providers.py`, so the web board, `/api/usage`, the usage
socket, the dispatcher pass and triage are all covered by one guard.

## Requirements

1. `GET /api/board` answers 200 when the usage cache cannot be written, and
   its `usage.providers` carries the reading the adapter just returned
   (source and windows), not `unavailable`.
2. `fetch_provider` catches an `OSError` raised while writing
   `usage/<provider>.json` and returns the adapter's reading unchanged; it
   never raises for a cache write failure. Callers outside the web
   (`fetch_all` in the dispatcher pass and triage) therefore never see it.
3. The write failure is logged by `fetch_provider` (a warning naming the
   provider), so the degraded cache is visible in the journal.
4. A failed write caches nothing: the next `fetch_provider` call for that
   provider within `MIN_POLL_SECONDS` calls the adapter again rather than
   serving a stale or partial cache.
5. When the cache is writable, behaviour is unchanged: a readable reading is
   written and a second call within `MIN_POLL_SECONDS` is served from the
   cache without calling the adapter.

## Scenarios

### Scenario: unwritable usage cache still yields a 200 board (reproduction)

- **Given** a state dir where `usage` is a plain file containing
  `not a directory` (so every cache write raises `OSError`, even as root),
  and an Anthropic adapter that returns a session reading at 0.42 used with
  source `oauth`
- **When** the operator calls `GET /api/board`
- **Then** the response status is 200 (today: 500)
- **And** `usage.providers` holds `anthropic` with source `oauth`
- **And** the `usage` file still contains `not a directory`

Test: `tests/test_web_sources.py::test_board_survives_an_unwritable_usage_cache`
(committed failing on this branch).

### Scenario: fetch_provider returns the reading when the write raises

- **Given** an adapter returning a reading with source `oauth` and one
  session window at 0.42 used, and a state dir whose cache write raises
  `OSError` (e.g. `usage` is a plain file)
- **When** `fetch_provider("anthropic", state_dir, now=…, adapters=…)` is called
- **Then** it returns that reading (source `oauth`, the same window)
- **And** it raises nothing

### Scenario: fetch_all in the pass survives an unwritable cache

- **Given** the same unwritable state dir and a config that routes to
  `anthropic`
- **When** `fetch_all(cfg, adapters=…)` is called
- **Then** it returns `{"anthropic": <the oauth reading>}` without raising

### Scenario: the write failure is logged

- **Given** the unwritable state dir and an adapter returning an `oauth` reading
- **When** `fetch_provider` is called
- **Then** a WARNING record from `dispatcher.usage_providers` names the
  provider `anthropic`

### Scenario: a failed write leaves no cache, so the next call refetches

- **Given** the unwritable state dir and a counting adapter
- **When** `fetch_provider` is called twice with the clock 10 s apart
  (well inside `MIN_POLL_SECONDS` = 180 s)
- **Then** the adapter is called twice

### Scenario: a writable cache still serves the second call (boundary)

- **Given** a writable state dir and a counting adapter returning an
  `oauth` reading
- **When** `fetch_provider` is called twice with the clock 10 s apart
- **Then** the adapter is called once and both calls return the same reading
- **And** `usage/anthropic.json` exists

(Existing tests in `tests/test_usage_providers.py` may already cover this
boundary; if so, no new test is needed for it.)

## Out of scope

- Cache read behaviour (`_cached` already treats unreadable files as a miss).
- Changes to `write_json_atomic`.
- Operator alerting beyond the log line.
- Catching non-`OSError` exceptions around the write.
