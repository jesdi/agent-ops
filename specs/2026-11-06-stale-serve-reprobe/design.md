# Stale-serve re-probe cadence — Design

Issue: #83.

## Decisions

- **Record the probe, not the success.** The TTL clock (`at` / `fetched_at`) means "last time `gh` was asked". A failed fetch refreshes it; the payload (`data`, `rows`, `as_of`, the description's `fetched_at`) is untouched. Tradeoff: the entry no longer says when `gh` last succeeded; the payload's own timestamps still do.
- **Failure marker lives in the cache entry.** The rank entry gains `stale: bool` (False after a success, True after any failure); a cache hit returns it instead of the literal `False`. The description's negative entry is recognised by its payload `error != ""`, so no new field is needed there. Tradeoff: one more key on the rank entry versus recomputing from state.
- **Per-entry lifetime for descriptions.** The description entry carries its own `ttl` (`DESCRIPTION_TTL_SECONDS` after success or stale refresh, `DESCRIPTION_RETRY_SECONDS` for a cold failure); the freshness check uses it. Tradeoff: one more key versus a second cache dict.
- **Two parallel edits, no shared helper** (spec out of scope). Tradeoff: ~6 duplicated lines.
- **`DESCRIPTION_RETRY_SECONDS = 30`** is a proposal, see the review page's open question.
- No lock: `Sources` caches are already unsynchronised; a concurrent double probe at expiry is no worse than today.

## Data model

In-memory dicts only, no persistence or DB constraints.

| Cache | Key | Entry |
|---|---|---|
| `_rank_cache` | project name | `rows`, `as_of`, `fetched_at` (probe time), `stale` |
| `_desc_cache` | `(repo, number)` | `data`, `at` (probe time), `ttl` |

## Identities

None. Keys are unchanged.

## Seams

- `Sources.issue_description(repo, number)` — existing.
- `Sources.rank_rows(repositories)` — existing.
Both driven with a fake `github` and injected clock, as the existing cache tests do.
