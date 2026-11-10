# Back off after an OAuth 429 — Design

## Decisions

- **Cooldown per token, not per account** (operator, 2026-11-10) — keeps the b8925be fall-through
  to a live host token; costs one request per token per cooldown instead of one per account.
- **One marker file per provider, one entry per token** — one read and one atomic rename per
  fetch, no directory to sweep; two writers can lose each other's entry (one extra request).
- **Entry key is a digest of the token, never the token** — a state-dir file is readable by other
  units (`test_the_cache_file_is_readable_by_other_units`), so no secret lands there. The key is
  `hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]`: 64 bits tell a box's few tokens apart.
- **Prune on write** — OAuth access tokens rotate, so entries that no longer gate are dropped when
  an entry is written; a marker nobody writes again stays as it is and simply stops gating.
- **Gate on read with `at < until <= at + MAX_COOLDOWN_SECONDS`** — a clock stepped back, or a
  forged huge `until`, can block a token for at most an hour.
- **ccusage is skipped only when every candidate is cooling or 429'd** — a 429 on the only token
  stays as quiet as the issue asked; a 429 beside a 403 or a timeout still gets today's fallback
  (open question on the review page).
- **`UsageFetchError` carries `status` and `retry_after`** — both optional keyword attributes, so
  every existing `UsageFetchError("...")` and every `except UsageFetchError` keeps working;
  `OpenAIUsage` ignores them.
- **`Retry-After` is ASCII digits only** — `-5`, `abc`, `²` (which `str.isdigit` accepts and
  `int` rejects) and HTTP-dates read as absent; `HTTPError.headers` may be `None`.
- **A marker write that raises `OSError` is logged and skipped** — the fall-through to the next
  token matters more than recording the cooldown; that token is simply asked again next pass.
- **`fetch_provider` is not touched** — failures stay uncached; the cooldown lives inside the
  adapter, under the cache, so a fresh good reading is still served during a cooldown.

## Data model

No database. One JSON file per provider beside the usage cache.

| File | Field | Constraint | Why |
|---|---|---|---|
| `usage/<provider>.cooldown.json` | top level | a JSON object; anything else reads as no cooldown | a corrupt file never blocks or raises |
| | key | `sha256(token.encode("utf-8")).hexdigest()[:16]` | no raw token on disk |
| | value `until` | a number (not a bool) with `at < until <= at + 3600` to gate | expired or out-of-range entries never block |
| | entries | only gating entries survive a write | rotated tokens do not accumulate |

Shape (example): `{"3f2a9c0d1e4b5a67": 1300.0}`.

## Identities

The cooldown's key widens from the provider to (provider, token).

- `usage/<provider>.cooldown.json` file name — per provider; covered by 01.
- entry key, the token digest — covered by 01.
- `usage/<provider>.json` (`cache_path`) — out of scope: the cache stays per provider, unchanged.
- `web/sources.py` digest of `usage/*.json` — out of scope: it hashes file contents for the SSE
  `usage` ping; a marker write sends one extra ping, harmless.
- `web/box_status.py` — out of scope: reads only `cache_path`.

## Seams

- `dispatcher.usage_providers._http_get_json` with `urllib.request.urlopen` patched — existing.
  Drives the status and `Retry-After` parsing (ticket 01).
- `dispatcher.usage_providers.AnthropicUsage(...).fetch(state_dir, now=...)` with
  `_http_get_json` and `_ccusage_json` patched — existing. Drives the per-token gate, the marker
  writes and the ccusage rule (ticket 01).
- `dispatcher.usage_providers.fetch_provider("anthropic", state_dir, now=..., adapters=...)` —
  existing. Drives the pass-to-pass timing scenarios, the cache interplay and the reproduction
  test (ticket 01).
- `dispatcher.usage_providers.OpenAIUsage().fetch(...)` — existing; the untouched check (ticket 01).
