# Back off after an OAuth 429 — Spec

Source: issue #1, rewritten 2026-11-05 against `61dbbdb`. Every decision below is the issue's, except
that the cooldown is per token (operator, 2026-11-10); this spec reconciles it with `main` at `0bf826c`. All changes are in
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
3. **Marker path and shape.** A helper `cooldown_path(state_dir, provider)` returns
   `<state_dir>/usage/<provider>.cooldown.json`, a different file from `cache_path`, so
   `_cached` never reads it. The marker is one JSON object mapping a per-token key to that
   token's `until` (epoch seconds). The key is derived from the token by a one-way digest; the
   raw token never appears in the file.
4. **Cooldown gates each token's request.** In `AnthropicUsage.fetch`, with `at = now()`: a
   candidate token whose marker entry has `at < until <= at + MAX_COOLDOWN_SECONDS` is skipped
   with no HTTP request; the loop goes on to the next token. A missing, unreadable or malformed
   marker means no token is cooling; a malformed or out-of-range entry means that token is not
   cooling. Neither ever raises.
5. **A 429 cools down that token only and falls through.** In the token loop, when the caught
   `UsageFetchError` has `status == 429`, record that token's entry as
   `until = at + min(retry_after or RATE_LIMIT_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS)` with
   `write_json_atomic(...)`, keeping the other tokens' live entries, then go on to the next
   token as today (b8925be: one store's token can be 429'd while another's is live).
   `Retry-After: 0` falls back to the 300 s default. When the loop ends without a reading and
   every candidate (at least one) was skipped as cooling or answered 429, return
   `unavailable(self.name, at)` without calling `ccusage`.
6. **Every other failure is unchanged.** A timeout, a 403, a 5xx, bad JSON or an empty reading
   writes no entry and goes on as today: next token, then `ccusage`, then `unavailable`. When
   the loop ends with at least one candidate that failed for such a reason (not cooling, not
   429), `ccusage` runs as today. No candidate token at all also runs `ccusage` as today.
5a. **A failed marker write does not end the read.** When writing the entry raises `OSError`,
   the failure is logged and the loop goes on to the next token as if the entry had been
   written (open question `marker-write-fails` on the plan review).
6a. **The marker stays small.** Writing an entry drops every entry that no longer gates
   (`until <= at` or `until > at + MAX_COOLDOWN_SECONDS`), so rotated tokens do not accumulate.
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

- **Given** valid credentials as the only candidate token (`credentials_path` given, no
  `CLAUDE_CODE_OAUTH_TOKEN`), `urllib.request.urlopen` raising
  `HTTPError(url, 429, "Too Many Requests", <no headers>, None)` and `_ccusage_json` returning
  `None`
- **When** `fetch_provider("anthropic", ...)` runs at t = 1000 and again at t = 1005
- **Then** both return `source == "unavailable"`, and exactly one request was made.
  Today: two requests (the committed red test
  `test_a_429_spaces_the_next_oauth_request`).

### Scenario: a 429 writes the marker and skips ccusage (requirements 3, 5)

- **Given** one token (`tok-a`), `_http_get_json` raising `UsageFetchError("x", status=429)`
  and a `_ccusage_json` that records its calls
- **When** `AnthropicUsage(credentials_path=...).fetch(tmp_path, now=lambda: 1000)` runs
- **Then** it returns `source == "unavailable"`, `_ccusage_json` was never called, and
  `<tmp_path>/usage/anthropic.cooldown.json` holds exactly one entry, with `until == 1300`,
  and its text does not contain `tok-a`.

### Scenario: a 429 falls through to the next token (requirements 4, 5; b8925be)

- **Given** a claude-home store token `tok-home` and a host store token `tok-host`, and
  `_http_get_json` raising `UsageFetchError("x", status=429)` for `tok-home` and returning a
  good reading for `tok-host`
- **When** `AnthropicUsage().fetch` runs at t = 1000
- **Then** it returns `source == "oauth"`, requests were made with `tok-home` then `tok-host`,
  and the marker holds one entry (for `tok-home`)
- **When** `AnthropicUsage().fetch` runs again at t = 1005
- **Then** exactly one request was made, with `tok-host`, and the result is `oauth`.

### Scenario: every token cooling makes no request and no ccusage (requirements 4, 5)

- **Given** two tokens, both with marker entries `until == 1300`, and a `_ccusage_json` that
  records its calls
- **When** `AnthropicUsage.fetch` runs at t = 1000
- **Then** no request was made, `_ccusage_json` was never called, and the result is
  `unavailable`.

### Scenario: a 429 beside another failure still runs ccusage (requirements 5, 6)

- **Given** an env token answering 429 and a store token answering 403
  (`UsageFetchError("x", status=403)`), and `_ccusage_json` returning a session block
- **When** `AnthropicUsage.fetch` runs at t = 1000
- **Then** both tokens were requested, the result has `source == "ccusage"`, and the marker
  holds one entry (for the env token).

### Scenario: no candidate token still runs ccusage (requirement 6)

- **Given** no env token and no credential store, and `_ccusage_json` returning a session block
- **When** `AnthropicUsage.fetch` runs
- **Then** no request was made, `_ccusage_json` was called, and the result is `ccusage`.

### Scenario: a failed marker write still falls through (requirement 5a)

- **Given** `tok-home` answering 429, `tok-host` answering a good reading, and
  `usage/anthropic.cooldown.json` unwritable (`write_json_atomic` raising `OSError`)
- **When** `AnthropicUsage().fetch` runs at t = 1000
- **Then** it returns `source == "oauth"` after requests with `tok-home` then `tok-host`, and
  raises nothing.

### Scenario: a 429 keeps the other tokens' live entries and prunes dead ones (requirements 5, 6a)

- **Given** a marker holding a live entry for `tok-b` (`until == 1200`) and an expired entry
  for a rotated token (`until == 900`) and an entry past the cap (`until == 5000`), and `tok-a`
  answering 429 at t = 1000
- **When** `AnthropicUsage.fetch` runs with candidates `tok-a` and `tok-b`
- **Then** the marker holds exactly two entries: `tok-a` with `until == 1300` and `tok-b` with
  `until == 1200`; the expired and past-cap entries are gone.

### Scenario: default cooldown boundary (requirements 1, 4, 7)

- **Given** a 429 with no `retry_after` at t = 1000
- **When** `fetch_provider` runs again at t = 1060
- **Then** it makes no request and returns `unavailable`
- **When** it runs at t = 1300
- **Then** it makes a request (`at < until` is false at the boundary).

### Scenario: Retry-After sets the cooldown (requirements 4, 5)

- **Given** a 429 with `retry_after=120` at t = 1000
- **When** `fetch_provider` runs at t = 1119
- **Then** no request is made
- **When** it runs at t = 1121
- **Then** a request is made.

### Scenario: an oversized Retry-After is capped (requirements 1, 5)

- **Given** a 429 with `retry_after=999999` at t = 1000
- **When** `fetch_provider` runs at t = 4601
- **Then** a request is made, and the entry written at t = 1000 had `until == 4600`.

### Scenario: Retry-After 0 uses the default (requirement 5)

- **Given** a 429 with `retry_after=0` at t = 1000
- **When** `fetch_provider` runs at t = 1299, then at t = 1301
- **Then** no request is made at t = 1299, a request is made at t = 1301 (entry
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
  `[]`, `{"until": 1100}` from an older shape, the token's own entry set to `"soon"`, `null`
  or `true`, and the marker path being a directory so reading it raises `OSError`), and a
  working endpoint
- **When** `AnthropicUsage.fetch` runs
- **Then** it makes a request, returns `source == "oauth"`, and raises nothing.

### Scenario: a marker past the cap does not block (clock stepped back) (requirement 4)

- **Given** a marker entry for the token with `until == 1000 + 3601` and a working endpoint
- **When** `AnthropicUsage.fetch` runs at t = 1000
- **Then** it makes a request and returns `source == "oauth"`.
- **And given** an entry with `until == 1000 + 3600`
- **Then** at t = 1000 no request is made (the boundary itself still blocks).

### Scenario: an expired marker does not block (requirement 4)

- **Given** a marker entry for the token with `until == 1000`
- **When** `AnthropicUsage.fetch` runs at t = 1000
- **Then** it makes a request (`at < until` is false).

### Scenario: a fresh cached reading is served during cooldown (requirement 7)

- **Given** a good OAuth reading cached by `fetch_provider` at t = 1000, then a marker entry
  for the token with `until == 1300`
- **When** `fetch_provider` runs at t = 1100
- **Then** it returns the cached `source == "oauth"` reading without calling the adapter.

### Scenario: _http_get_json keeps status and Retry-After (requirement 2)

- **Given** `urllib.request.urlopen` raising `HTTPError(url, 429, ..., hdrs, None)` with
  `hdrs["Retry-After"] = "90"`
- **When** `_http_get_json` runs
- **Then** it raises `UsageFetchError` with `status == 429` and `retry_after == 90`.
- **And** with `Retry-After: 0`, `retry_after == 0` (the boundary is a valid value).
- **And** with `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`, `-5`, `abc`, `²`, no header, or
  an `HTTPError` whose `hdrs` is `None`,
  `retry_after is None` and `status == 429`.
- **And** a `URLError("down")`, a `TimeoutError` and a body that is not JSON each raise
  `UsageFetchError` with `status is None` and `retry_after is None`.

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
  host's `~/.claude`). The issue had a 429 stop the whole read ("they hit the same account
  limit"), but `b8925be` and its test `test_refused_claude_home_token_falls_through_to_host_store`
  pin the box's 2026-09-23 case: the endpoint 429'd the claude-home token while the host
  token was live. The operator chose (2026-11-10) a per-token cooldown that falls through to
  the next token, so the credential stores are still read during a cooldown.
- **`write_json_atomic`** now lives in `dispatcher/state.py` (`8c6fe96`) and is already imported
  here; the marker uses it, so the file is written aside and renamed like the cache.
- **Other readers of `usage/`.** `web/box_status.py` (`agent-ops status`, from
  `specs/2026-11-09-box-status`) reads only `cache_path(...)`, so it never sees the marker.
  `web/sources.py` digests `usage/*.json` for the console's `usage` SSE key, so writing a
  marker sends one extra harmless `usage` ping. Neither needs a change.
- **Two processes.** The dispatcher pass and the console (`/api/board`, `fetch_all`) both call
  `fetch_provider` on the same state dir. Because the marker is a file, a 429 seen by either
  one spaces the other's requests too; this is wanted, not a side effect to guard against. Both
  read, change and rename the same marker; if they write at the same moment, the last rename
  wins and the other's entry is lost, which costs one extra request after the next 429, never
  a wrong reading.
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
