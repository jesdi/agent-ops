# Usage: drop the ccusage fallback so a dead OAuth token reads as unavailable

## Why

`AnthropicUsage.fetch` in `dispatcher/usage_providers.py` reads usage as
"OAuth endpoint → ccusage → unavailable". When every OAuth token is rejected
(the token the fleet needs is dead), the adapter still shells out to
`ccusage blocks --json` and, when a `ccusage` executable on `PATH` answers
with an active block, returns a healthy-looking reading with
`source == "ccusage"` and a session window. `ccusage` reads local JSONL
logs and proves nothing about whether the OAuth token is live.

Root cause: the fallback was designed for an OAuth *outage*, not for a dead
*credential*, and nothing downstream can tell the two apart. Two consumers
act only on `source == "unavailable"`:

1. The auth-dark detector (`_auth_dark_edge` in `dispatcher/main.py`) sees a
   usable reading, clears its marker and never alerts the operator.
2. The usage gate sees a session window and admits spawns; the sessions it
   launches hit the login prompt at once.

Today the path is dormant (`ccusage` is not provisioned on the box) but it
fails exactly when the box changes. The auth-resilience spec already lists
the ccusage fallback under "Not doing". The reproduction is committed first
as `test_a_dead_token_is_unavailable_even_if_ccusage_would_answer` in
`tests/test_usage_providers.py`: with the endpoint rejecting the token and a
fake `ccusage` first on `PATH`, the reading comes back `ccusage`, not
`unavailable`.

## For whom

The operator of the box, who relies on the auth-dark alert to learn that a
re-login is due, and the dispatcher's spawn gate, which must fail closed
when usage cannot be known.

## Goal

The Anthropic usage adapter reads "OAuth endpoint → unavailable": once every
token is rejected or yields no windows, the reading is `unavailable` with no
windows, whatever is installed on the box. The ccusage code and its tests
are gone, and a test guards against the fallback returning.

## Non-goals

- Removing `"ccusage"` from the `Source` literal in `dispatcher/usage.py`,
  from `frontend/src/lib/api-types.ts`, the "via ccusage · weekly unknown"
  label in `frontend/src/components/UsagePanel.tsx`, and the related
  frontend and `tests/test_usage.py` fixtures. They become unreachable but
  harmless; a follow-up cleans them up.
- Editing `docs/specs/2026-09-13-usage-pace-gate-design.md`, which still
  describes ccusage (`specs/` and `docs/specs/` are a change log).
- Changes to `_auth_dark_edge` or the spawn gate: both are correct once
  `unavailable` is reported.
- OAuth 429 backoff (#1).
- Any new fallback source for usage.
