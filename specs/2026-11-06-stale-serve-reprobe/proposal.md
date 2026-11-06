# Stale-serve re-probe cadence — Proposal

Issue: #83

## Why

`Sources.issue_description` and `Sources.rank_rows` (`web/sources.py`) keep a
TTL cache in front of `gh`. When a fetch fails they serve the last cached value
(stale beats blank) or, with a cold cache, an explicit error / empty-stale
answer. Neither failure path writes the cache: `issue_description` returns
`cached["data"]` without touching `cached["at"]`, and `rank_rows` returns
`cached["rows"]` without touching `cached["fetched_at"]`; a cold failure stores
nothing at all.

Root cause: the cache timestamp records the last *success*, and the TTL check
compares against it, so once the TTL has lapsed during an outage the entry is
expired forever. Every later request falls through to `gh`, and
`issue_view` / `rank_rows` shell out through `_run(..., timeout=120)`. A
sustained `gh` outage therefore turns each console request into a fresh, up to
120 s subprocess — the opposite of what the 300 s description TTL and 15 s rank
TTL are for. Reproduced by
`tests/test_web_sources.py::test_issue_description_outage_reprobes_at_ttl_cadence_not_per_request`
and `::test_rank_rows_outage_reprobes_at_ttl_cadence_not_per_request`: five
requests during an outage make five `gh` calls instead of one.

## What changes

- A failed fetch is remembered. Serving stale refreshes the entry's probe time
  (not its data or its `as_of` / `fetched_at` payload), so the outage is
  re-probed once per TTL, not once per request.
- A cold-cache failure is negative-cached: `issue_description` caches its
  error payload for a short retry interval (`DESCRIPTION_RETRY_SECONDS`, 30 s)
  so a recovered `gh` shows within seconds, not after 300 s; `rank_rows`
  caches its empty-stale answer for `RANK_TTL_SECONDS` (already short).
- A stale or negative-cached answer keeps reporting itself as such while it is
  served from cache (`rank_rows` stays `stale=True`; the description keeps its
  `error`).

## Out of scope

- `GitHubClient._run`'s 120 s timeout and the other gh callers (`issue_open`,
  dispatcher reads): they are not request-path caches.
- Sharing one cache helper between the two methods; two small, parallel edits
  are enough.

## Impact

- `web/sources.py` only (`rank_rows`, `issue_description`, one new constant).
- Tests in `tests/test_web_sources.py`. No route, payload-shape or UI change.
