# Stale-serve re-probe cadence — Spec

Issue: #83. Code: `web/sources.py` (`Sources.issue_description`,
`Sources.rank_rows`). Test seam: `Sources` with a fake `github` and an injected
clock, in `tests/test_web_sources.py` (as the existing cache tests do).

## Requirements

1. **Stale description re-probed at TTL cadence.** When `issue_description`
   finds its cached entry past `DESCRIPTION_TTL_SECONDS` and the `gh` fetch
   fails, it serves the cached data unchanged (same `title`, `body`, `url`,
   original `fetched_at`, `error == ""`) and records the probe time, so no
   further `gh` call is made for that `(repo, number)` until another
   `DESCRIPTION_TTL_SECONDS` have passed.
2. **Cold description failure negative-cached.** When `issue_description` has
   no cached entry and the fetch fails, it returns the explicit error payload
   (empty `title`/`body`/`url`, non-empty `error`, never a raise) and caches
   it; no further `gh` call is made for that key until
   `DESCRIPTION_RETRY_SECONDS` (new module constant, 30 s, shorter than the
   TTL) have passed. A successful fetch after that replaces the error payload
   and is cached for the normal TTL.
3. **Stale rank rows re-probed at TTL cadence.** When `rank_rows` finds its
   cached entry past `RANK_TTL_SECONDS` and the fetch fails, it returns the
   cached rows with the original `as_of` and `stale=True`, and records the
   probe time; requests within the next `RANK_TTL_SECONDS` return the same
   `(rows, as_of, True)` without calling `gh`.
4. **Cold rank failure negative-cached.** When `rank_rows` has no cached entry
   for the project and the fetch fails, it returns `([], <now iso>, True)` and
   caches that answer for `RANK_TTL_SECONDS`; requests in that window return
   `[]` with `stale=True` without calling `gh`.
5. **Stale is never laundered into fresh.** Any answer served from a cache
   entry that was written by a failure keeps its failure marker: `rank_rows`
   reports `stale=True`, a negative-cached description keeps its `error`. Only
   a successful fetch produces `stale=False` / `error == ""` from a fresh
   probe. Existing behaviour on success (cache for the TTL, `stale=False`,
   cache keyed by `(repo, number)` / project name) is unchanged.

## Scenarios

### Scenario: R1 — Reproduction: outage on a warm description cache (#83)

- **Given** `jesdi/alpha#73` was fetched successfully (title `T`) and
  `DESCRIPTION_TTL_SECONDS + 1` have passed, and `gh issue view` now fails
- **When** the console requests that description five times in a row
- **Then** every response has title `T` and `error == ""`, and `gh` was called
  exactly once for those five requests (today: five times — one 120 s
  subprocess per request)
- **And when** another `DESCRIPTION_TTL_SECONDS + 1` pass and one more request
  arrives, `gh` is called exactly once more

### Scenario: R2 — Outage on a cold description cache

- **Given** `jesdi/alpha#99` was never fetched and `gh issue view` fails
- **When** it is requested five times in a row
- **Then** every response has an empty title and a non-empty `error`, and `gh`
  was called once

### Scenario: R2 — Recovery shows after the short retry interval

- **Given** the cold failure above was cached
- **When** `DESCRIPTION_RETRY_SECONDS + 1` pass, `gh` recovers, and `#99` is
  requested
- **Then** `gh` is called once, the response has its title and `error == ""`,
  and a request 100 s later is served from cache without a `gh` call

### Scenario: R3 — Outage on a warm rank cache

- **Given** project `alpha`'s rank rows were fetched successfully at `as_of` A
  and `RANK_TTL_SECONDS + 1` have passed, and `gh` now fails
- **When** the rank rows are requested five times
- **Then** each returns the cached rows, `as_of` A and `stale=True`, and `gh`
  was called once
- **And when** another `RANK_TTL_SECONDS + 1` pass and one more request
  arrives, `gh` is called exactly once more

### Scenario: R4 — Outage on a cold rank cache

- **Given** project `beta` was never fetched and `gh` fails
- **When** its rank rows are requested five times
- **Then** each returns `[]` with `stale=True`, and `gh` was called once

### Scenario: R5 — A stale serve is never reported as fresh

- **Given** a warm rank cache that went stale during an outage (R3)
- **When** a request lands inside the refreshed TTL window
- **Then** it returns `stale=True`, not `stale=False`
- **And given** a negative-cached description (R2), a request inside the retry
  window returns the same non-empty `error`

### Scenario: R1/R3 — Boundary: the refreshed window ends at exactly the TTL

- **Given** a warm description (or rank) cache that went stale during an
  outage and was re-probed once at time P
- **When** a request arrives at `P + TTL - 1` and another at `P + TTL`
  (`DESCRIPTION_TTL_SECONDS` / `RANK_TTL_SECONDS`)
- **Then** the first is served from cache with no `gh` call; the second
  re-probes `gh` once (same `<` comparison as the success path)

### Scenario: R5 — Success path unchanged

- **Given** `gh` is healthy
- **When** the existing tests `test_rank_rows_cached_within_ttl`,
  `test_rank_failure_serves_stale_cache`,
  `test_rank_failure_without_cache_is_empty_stale`,
  `test_issue_description_caches_and_degrades` and
  `test_issue_description_cache_key_includes_repo` run
- **Then** they pass unmodified

## Notes for the plan

- The reproduction tests are already committed (`test: reproduce …`); they
  read `DESCRIPTION_RETRY_SECONDS` through `web.sources` so the rest of the
  module still collects while the constant does not exist.
- `DESCRIPTION_RETRY_SECONDS = 30` is the spec session's proposal: the issue
  only asks for "a shorter interval". It is an open question on the review
  page.
- One ticket covers it: both methods, one new constant, no new module or
  helper.
