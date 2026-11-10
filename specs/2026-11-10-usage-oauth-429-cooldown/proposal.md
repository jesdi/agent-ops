# Back off after an OAuth 429 — Proposal

## Why

When the Anthropic OAuth usage endpoint answers HTTP 429, the next pass calls it again
straight away, so a tight loop (a manual `--once` loop, a shorter timer, the console's
`/api/board` refreshes) keeps hitting an endpoint that is rate-limiting it, and the 429s last
longer. Reproduced by `tests/test_usage_providers.py::test_a_429_spaces_the_next_oauth_request`
(committed red before this spec): two `fetch_provider("anthropic", ...)` calls 5 s apart, with
`urlopen` raising `HTTPError` 429, make two HTTP requests.

Root cause, three links in `dispatcher/usage_providers.py`, each correct alone:

1. `_http_get_json` turns every `urllib.error.URLError`, `HTTPError` 429 included, into a bare
   `UsageFetchError(str(e))`. The status code and the `Retry-After` header are lost, so no
   caller can tell a rate limit from any other failure.
2. `AnthropicUsage.fetch` catches that error with
   `except (UsageFetchError, KeyError, ValueError, TypeError): continue`, then tries the next
   token and `ccusage`. Nothing is recorded about the 429. Its comment ("the cache spaces passes
   ≥180s apart") holds only for successful readings.
3. `fetch_provider` deliberately never caches an `unavailable` reading ("an outage never masks
   a recovery"), so `MIN_POLL_SECONDS` (180 s) never spaces retries after a failure.

Nothing in the chain remembers a 429, so nothing spaces the retries. The fix records the 429 in
a cooldown marker that the adapter checks before it makes any request. Caching failures in
`fetch_provider` stays out: that design is correct and kept.

## For whom

The operator running the agent-ops box, and the box itself: the dispatcher pass and the console
both read usage through `fetch_provider`, and a usage endpoint that stays rate-limited leaves
the usage gate unable to admit work.

## Goal

After an OAuth 429, `AnthropicUsage.fetch` makes no further usage request until a cooldown
(the `Retry-After` header, or 300 s by default, capped at 3600 s) has passed. Every other
failure keeps today's behaviour.

## Non-goals

- Exponential backoff or escalating cooldowns on repeated 429s (a fixed or header-given
  cooldown is enough).
- Cooldowns for the OpenAI adapter or other status codes.
- Changing `fetch_provider` caching of failures, `MIN_POLL_SECONDS`, or the `ccusage` fallback
  for non-429 errors.
- Parsing HTTP-date `Retry-After` values.
- Deleting the marker on a successful reading; it simply expires.
