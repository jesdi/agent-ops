# Usage: drop the ccusage fallback so a dead OAuth token reads as unavailable — Design

## Decisions

- **Delete the fallback instead of gating it behind a flag** — nothing needs it (not provisioned on the box, listed under "Not doing" by the auth-resilience spec); a flag would keep the dead-credential hole one setting away.
- **Fix in `AnthropicUsage.fetch`, the one place every caller routes through** — `fetch_provider`, the auth-dark detector and the spawn gate all read its result, so no consumer changes.
- **Keep `"ccusage"` in the `Source` literal and the frontend** — unreachable but harmless; removing it widens the diff across the console for no behavior change (follow-up).
- **One ticket** — the change is a deletion in one module plus its tests; the reproduction test is already committed red, so splitting would leave no demoable half.
- **Red-team of the data model** — no data model change: no table, key, cache format or identity moves. The cache file `usage/anthropic.json` keeps its shape; old files with `source: "ccusage"` age out at the cache's min-poll and are never written again.

## Data model

None. No constraint added or changed.

## Identities

None.

## Seams

- `AnthropicUsage.fetch(state_dir)` — existing; every scenario but the cache and grep ones drives it.
- `fetch_provider("anthropic", state_dir, adapters=...)` — existing; the malformed-payload and never-cached scenarios.
- `git grep` and `make test` for the last two scenarios.
