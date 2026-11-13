# 0014. Anthropic OAuth 429 cooldown is per token

Date: 2026-11-13

## Context

An Anthropic usage request that returns HTTP 429 used to be retried on the next
pass. The adapter can try several OAuth tokens; one store's token may be rate
limited while another store's token is live. Both the dispatcher and console
read usage from shared state.

## Decision

- Cool down each token independently, preserving fallback to the next candidate.
  A 429 must not make all tokens appear rate limited.
- Store entries in one `usage/anthropic.cooldown.json` object, keyed by the first
  16 hex characters of SHA-256 of the UTF-8 token. Never write a raw token.
  Read and atomic rename of the object are sufficient; concurrent writers may
  lose one entry, causing one extra request.
- The default cooldown is 300 seconds when `Retry-After` is absent or zero;
  cap it at 3600 seconds. A marker gates only while
  `at < until <= at + 3600`, so clock steps or forged far-future values cannot
  block indefinitely. Missing, malformed, unreadable and out-of-range entries
  do not gate. On write, drop entries that no longer gate, since tokens rotate.
- If an atomic marker write raises `OSError`, log it and continue to the next
  token. A successful usage read leaves the marker intact until it expires.
- Keep the cooldown inside the Anthropic adapter. The provider cache continues
  to serve a fresh good reading and does not cache unavailable readings.

## Consequences

The first read after expiry retries the affected token. Other live tokens can
supply usage during its cooldown. A failed marker write permits a retry on the
next pass. ADR 0008 still governs the absence of a local-log fallback.
