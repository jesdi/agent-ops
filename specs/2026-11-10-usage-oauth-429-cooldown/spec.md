# Back off after an OAuth 429 — Spec

Source: issue #1, rewritten 2026-11-05 against `61dbbdb`. Every decision below is the issue's;
this spec reconciles it with `main` at `0bf826c`. All changes are in
`dispatcher/usage_providers.py`, all tests in `tests/test_usage_providers.py` (helpers `creds`,
`oauth_response`, `boom`, and the `monkeypatch.setattr(up, "_http_get_json", ...)` pattern).

## Requirements

1. **Constants.** Next to `MIN_POLL_SECONDS`: `RATE_LIMIT_COOLDOWN_SECONDS = 300` (the default
   when a 429 has no usable `Retry-After`) and `MAX_COOLDOWN_SECONDS = 3600` (the cap on any
   cooldown, so a bad header or clock step cannot lock the provider out for long).
2. **The error keeps the status.** `UsageFetchError` gets two optional attributes,
   `status: int | None = None` and `retry_after: float | None = None`; `UsageFetchError("x")`
   still works and has both `None`. `_http_get_json` catches `urllib.error.HTTPError` before
   the generic `URLError` and raises `UsageFetchError(str(e), status=e.code, retry_after=...)`,
   where `retry_after` is the `Retry-After` header parsed as a non-negative integer number of
   seconds, else `None`. An HTTP-date `Retry-After`, a missing header, or any other value that
   is not a non-negative integer gives `None`. Other `URLError`s, `TimeoutError` and
   `JSONDecodeError` raise as today (`status` and `retry_after` `None`).
3. **Marker path.** A helper `cooldown_path(state_dir, provider)` returns
   `<state_dir>/usage/<provider>.cooldown.json`, a different file from `cache_path`, so
   `_cached` never reads it.
4. **Cooldown gates the request.** In `AnthropicUsage.fetch`, after reading `at = now()`: if the
   marker exists with `at < until <= at + MAX_COOLDOWN_SECONDS`, return
   `unavailable(self.name, at)` with no HTTP request, no `ccusage` call and no credential read.
   A missing, unreadable, malformed or out-of-range marker means no cooldown, and never raises.
5. **A 429 writes the marker and stops.** In the token loop, when the caught `UsageFetchError`
   has `status == 429`, write the marker with `write_json_atomic(...)` as
   `{"until": at + min(retry_after or RATE_LIMIT_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS)}` and
   return `unavailable(self.name, at)` at once: no further token, no `ccusage` (they hit the
   same account limit). `Retry-After: 0` falls back to the 300 s default.
6. **Every other failure is unchanged.** A timeout, a 403, a 5xx, bad JSON or an empty reading
   writes no marker and goes on as today: next token, then `ccusage`, then `unavailable`.
7. **`fetch_provider` is unchanged.** The unavailable reading is still not cached, so the first
   pass after `until` makes a request again; a good cached reading younger than
   `MIN_POLL_SECONDS` is still served before the adapter runs (the cooldown gates only the
   network call). A successful reading deletes nothing.
8. **`OpenAIUsage` is untouched.** It still catches the (now richer) `UsageFetchError` and
   reads `unavailable`, with no marker.

## Scenarios

Times are seconds on an injected clock, starting at `t = 1000`. "Request" means a call to
`up._http_get_json` (or, in the first scenario, to `urllib.request.urlopen`).

### Scenario: reproduction: a 429 is not retried on the next pass (requirements 2, 4, 5)

- **Given** valid credentials, `urllib.request.urlopen` raising
  `HTTPError(url, 429, "Too Many Requests", <no headers>, None)` and `_ccusage_json` returning
  `None`
- **When** `fetch_provider("anthropic", ...)` runs at t = 1000 and again at t = 1005
- **Then** both return `source == "unavailable"`, and exactly one request was made.
  Today: two requests (the committed red test
  `test_a_429_spaces_the_next_oauth_request`).

### Scenario: a 429 writes the marker and skips ccusage (requirement 5)

- **Given** `_http_get_json` raising `UsageFetchError("x", status=429)` and a `_ccusage_json`
  that records its calls
- **When** `AnthropicUsage(credentials_path=...).fetch(tmp_path, now=lambda: 1000)` runs
- **Then** it returns `source == "unavailable"`, `_ccusage_json` was never called, and
  `<tmp_path>/usage/anthropic.cooldown.json` exists with `until == 1300`.

### Scenario: a 429 stops the token loop (requirement 5)

- **Given** an env token and a credentials-store token, and `_http_get_json` raising
  `UsageFetchError("x", status=429)` for every token
- **When** `AnthropicUsage.fetch` runs
- **Then** exactly one request was made.

### Scenario: default cooldown boundary (requirements 1, 4, 7)

- **Given** a 429 with no `retry_after` at t = 1000
- **When** `fetch_provider` runs again at t = 1060
- **Then** it makes no request and returns `unavailable`
- **When** it runs at t = 1301
- **Then** it makes a request.

### Scenario: Retry-After sets the cooldown (requirements 4, 5)

- **Given** a 429 with `retry_after=120` at t = 1000
- **When** `fetch_provider` runs at t = 1119
- **Then** no request is made
- **When** it runs at t = 1121
- **Then** a request is made.

### Scenario: an oversized Retry-After is capped (requirements 1, 5)

- **Given** a 429 with `retry_after=999999` at t = 1000
- **When** `fetch_provider` runs at t = 4601
- **Then** a request is made, and the marker written at t = 1000 had `until == 4600`.

### Scenario: Retry-After 0 uses the default (requirement 5)

- **Given** a 429 with `retry_after=0` at t = 1000
- **When** `fetch_provider` runs at t = 1299, then at t = 1301
- **Then** no request is made at t = 1299, a request is made at t = 1301 (marker
  `until == 1300`).

### Scenario: other failures write no marker and retry at once (requirement 6)

- **Given** `_http_get_json` raising, in turn, `UsageFetchError("x", status=403)`,
  `UsageFetchError("x", status=500)` and `UsageFetchError("timed out")` (no status), with
  `_ccusage_json` returning `None`
- **When** `fetch_provider` runs at t = 1000 and again at t = 1005 for each
- **Then** no `anthropic.cooldown.json` exists, the second call makes a request again, and
  `_ccusage_json` was called on each fetch. `test_unavailable_is_never_cached` and the existing
  ccusage-fallback tests still pass.

### Scenario: a corrupt marker does not block (requirement 4)

- **Given** `usage/anthropic.cooldown.json` holding `not json` (and, as separate cases, `{}`,
  `{"until": "soon"}`, `[]`), and a working endpoint
- **When** `AnthropicUsage.fetch` runs
- **Then** it makes a request, returns `source == "oauth"`, and raises nothing.

### Scenario: a marker past the cap does not block (clock stepped back) (requirement 4)

- **Given** a marker with `until == 1000 + 3601` and a working endpoint
- **When** `AnthropicUsage.fetch` runs at t = 1000
- **Then** it makes a request and returns `source == "oauth"`.
- **And given** a marker with `until == 1000 + 3600`
- **Then** at t = 1000 no request is made (the boundary itself still blocks).

### Scenario: an expired marker does not block (requirement 4)

- **Given** a marker with `until == 1000`
- **When** `AnthropicUsage.fetch` runs at t = 1000
- **Then** it makes a request (`at < until` is false).

### Scenario: cooldown reads no credentials (requirement 4)

- **Given** a marker with `until == 1100` and a `credentials_path` that does not exist,
  `_read_token` patched to record calls
- **When** `AnthropicUsage.fetch` runs at t = 1000
- **Then** `_read_token` was never called and the result is `unavailable`.

### Scenario: a fresh cached reading is served during cooldown (requirement 7)

- **Given** a good OAuth reading cached by `fetch_provider` at t = 1000, then a marker with
  `until == 1300`
- **When** `fetch_provider` runs at t = 1100
- **Then** it returns the cached `source == "oauth"` reading without calling the adapter.

### Scenario: _http_get_json keeps status and Retry-After (requirement 2)

- **Given** `urllib.request.urlopen` raising `HTTPError(url, 429, ..., hdrs, None)` with
  `hdrs["Retry-After"] = "90"`
- **When** `_http_get_json` runs
- **Then** it raises `UsageFetchError` with `status == 429` and `retry_after == 90`.
- **And** with `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`, `-5`, `abc` or no header,
  `retry_after is None` and `status == 429`.
- **And** a `URLError("down")` raises `UsageFetchError` with `status is None`.

### Scenario: OpenAI is untouched (requirement 8)

- **Given** `_http_get_json` raising `UsageFetchError("x", status=429)` for the OpenAI adapter
- **When** `OpenAIUsage.fetch` runs twice
- **Then** both return `unavailable`, both make a request, and no `openai.cooldown.json` exists.

### Scenario: the full suite passes

- **When** the repository gate (`make gate`) runs
- **Then** it passes, including every pre-existing test in `tests/test_usage_providers.py`.

## What moved since the issue was written

The issue was written at `61dbbdb`; reconciled against `0bf826c`:

- **Line numbers.** `_http_get_json`, `AnthropicUsage.fetch`, `fetch_provider` and
  `cache_path` are found by name; their shape is as the issue describes. The swallowing
  `except (...): continue` is still there, now commented "the cache spaces passes ≥180s apart",
  which this change makes true for 429s.
- **Token sources.** `fetch` no longer reads a setup token from 1Password (`a013377`); its
  candidates are `CLAUDE_CODE_OAUTH_TOKEN` and the credential stores (claude-home, then the
  host's `~/.claude`). Requirement 4's "no credential read" therefore means `_read_token` is not
  called and `_credential_stores` is not walked: the marker check comes before the
  `candidates` list is built, which today precedes `at = now()`. The design moves `at = now()`
  first; the clock is injected and read once either way.
- **`write_json_atomic`** now lives in `dispatcher/state.py` (`8c6fe96`) and is already imported
  here; the marker uses it, so the file is written aside and renamed like the cache.
- **Other readers of `usage/`.** `web/box_status.py` (`agent-ops status`, from
  `specs/2026-11-09-box-status`) reads only `cache_path(...)`, so it never sees the marker.
  `web/sources.py` digests `usage/*.json` for the console's `usage` SSE key, so writing a
  marker sends one extra harmless `usage` ping. Neither needs a change.
- **Two processes.** The dispatcher pass and the console (`/api/board`, `fetch_all`) both call
  `fetch_provider` on the same state dir. Because the marker is a file, a 429 seen by either
  one spaces the other's requests too; this is wanted, not a side effect to guard against.
- **#75** (drop the `ccusage` fallback) is still open. Whichever lands second drops the
  `ccusage` references here (requirement 5's "no `ccusage`" and the ccusage assertions in the
  scenarios).

## Out of scope

- Exponential backoff or escalating cooldowns on repeated 429s.
- Cooldowns for the OpenAI adapter or other status codes.
- Changing `fetch_provider` caching of failures, `MIN_POLL_SECONDS`, or the `ccusage` fallback
  for non-429 errors.
- Parsing HTTP-date `Retry-After` values.
- How the console or `agent-ops status` labels an `unavailable` reading caused by a cooldown
  (today it reads as unknowable usage, the same as any other failure).
