# Usage: drop the ccusage fallback so a dead OAuth token reads as unavailable — Spec

Terms:

- **Dead token**: every candidate token (the `CLAUDE_CODE_OAUTH_TOKEN`
  override, then each OAuth store) is rejected by the usage endpoint, or
  the endpoint answers with a payload that yields no windows.
- **Reading**: the `ProviderUsage` that `AnthropicUsage.fetch` returns.
- **Unavailable reading**: `source == "unavailable"` and `windows == ()`.

## Requirements

1. **A dead token reads as unavailable, whatever is on `PATH`.** When every
   token is rejected, `AnthropicUsage.fetch` returns an unavailable reading
   even when a working `ccusage` executable is first on `PATH`. The adapter
   never runs `ccusage`.
2. **The ccusage code is gone.** `_ccusage_json`, `_parse_ccusage`, the
   `"ccusage"` return in `fetch` and the "then ccusage" wording in its
   docstring and loop comment are removed. `git grep -n "_ccusage_json\|_parse_ccusage"`
   finds nothing under `dispatcher/` or `tests/`. The `fetch` docstring reads
   "OAuth endpoint → unavailable"; the loop comment reads "next token; the
   cache spaces passes ≥180s apart". `timedelta` leaves the `datetime` import
   and `subprocess` leaves the imports when nothing else in the module uses
   them (the issue says to keep `subprocess` for `_token_from_op`, but that
   function no longer exists on `main`: `_ccusage_json` is the only user
   left, so grep and drop both).
3. **A live token still reads as oauth.** With a token the endpoint accepts,
   `fetch` returns `source == "oauth"` with the same windows as before. The
   existing oauth tests pass unchanged.
4. **Every other failure still reads as unavailable.** A 200 with no windows
   (after trying the next token), a malformed payload and missing
   credentials each yield an unavailable reading, never a raise out of
   `fetch_provider`.
5. **Unavailable is never cached.** `fetch_provider` writes no cache file for
   an unavailable reading, so a recovery is seen on the next pass.
6. **The ccusage tests go with the code.** The three ccusage-only tests
   (`test_falls_back_to_ccusage_with_a_session_window_only`,
   `test_an_empty_oauth_reading_falls_back_to_ccusage`,
   `test_ccusage_used_is_clamped_and_resets_follow_the_injected_clock`) are
   deleted; every `monkeypatch.setattr(up, "_ccusage_json", ...)` line is
   removed, and a test whose `monkeypatch` becomes unused drops it from its
   signature. The reproduction test from requirement 1 stays as the guard. Test
   imports that the deletions leave unused (`timedelta`, `WindowKind`, ...) are
   dropped, and the docstring of `test_an_empty_oauth_reading_is_not_oauth`
   loses "then ccusage".
7. **The suite and the gate pass.** `make test` passes; `make gate` is green
   before the PR.

## Scenarios

### Scenario: a dead token with a working ccusage on PATH is unavailable (reproduction)

- **Given** a credentials file holding access token `tok-123`, the usage
  endpoint rejecting every request (`_http_get_json` raises
  `UsageFetchError("dark")`), and an executable `ccusage` first on `PATH`
  that prints `{"blocks": [{"isActive": true, "projection":
  {"remainingMinutes": 45}, "percentUsed": 62.0}]}`
- **When** `AnthropicUsage(credentials_path=...).fetch(state_dir)` runs
- **Then** the reading has `source == "unavailable"` and `windows == ()`
  (today: `source == "ccusage"` with one session window at 62% used,
  resetting in 45 minutes)

### Scenario: a live token reads as oauth with its windows

- **Given** the same credentials file and the endpoint answering with the
  `tests/fixtures/anthropic-usage.json` payload
- **When** `fetch` runs
- **Then** `source == "oauth"` and the windows are session (5%), weekly
  (13%) and weekly scoped to `Fable` (23%), as today

### Scenario: a 200 with no windows tries the next token, then is unavailable

- **Given** `CLAUDE_CODE_OAUTH_TOKEN=env-tok` and a credentials file with
  `tok-123`, the endpoint answering `{"limits": []}` for `env-tok` and the
  fixture payload for `tok-123`
- **When** `fetch` runs
- **Then** `source == "oauth"` with three windows
- **And given** the endpoint answers `{"limits": []}` (or `{}`) for every
  token
- **Then** the reading is unavailable with no windows

### Scenario: a malformed payload is unavailable, not a raise

- **Given** the endpoint answering `{"limits": {"kind": "session"}}`,
  `{"limits": ["session"]}`, a limits entry whose `resets_at` is the integer
  `1757764799`, or `["not", "an", "object"]`
- **When** `fetch_provider("anthropic", state_dir, adapters=...)` runs
- **Then** the reading is unavailable and nothing is raised

### Scenario: missing credentials are unavailable

- **Given** `credentials_path` pointing at a file that does not exist and no
  `CLAUDE_CODE_OAUTH_TOKEN`
- **When** `fetch` runs
- **Then** the reading is unavailable

### Scenario: an unavailable reading is never cached

- **Given** the endpoint rejecting every token
- **When** `fetch_provider("anthropic", state_dir, adapters=...)` runs
- **Then** the reading is unavailable and `<state_dir>/usage/anthropic.json`
  does not exist

### Scenario: no ccusage symbol survives

- **Given** the change applied
- **When** `git grep -n "_ccusage_json\|_parse_ccusage" -- dispatcher tests`
  runs
- **Then** it prints nothing and exits 1

### Scenario: the suite is green

- **Given** the change applied
- **When** `make test` runs
- **Then** every test passes, including
  `test_a_dead_token_is_unavailable_even_if_ccusage_would_answer`

## Out of scope

- Removing `"ccusage"` from the `Source` literal (`dispatcher/usage.py`),
  `frontend/src/lib/api-types.ts`, the `UsagePanel` "via ccusage · weekly
  unknown" label, and the related frontend and `tests/test_usage.py`
  fixtures. Unreachable but harmless; follow-up.
- Editing `docs/specs/2026-09-13-usage-pace-gate-design.md`.
- Changes to `_auth_dark_edge` or the spawn gate.
- OAuth 429 backoff (#1).
