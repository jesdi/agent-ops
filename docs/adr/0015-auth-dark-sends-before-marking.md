# 0015. Auth dark sends before marking

Date: 2026-11-10

## Context

Telegram delivery and marker persistence cannot be made atomic together.

## Decision

- **Send first, then write the marker** (as today) — a `send` that raises leaves the marker
  as it was, so the next pass retries; a write that fails after a send can repeat one alert on
  the next pass, the cheaper failure.

## Consequences

A failed send remains due for retry; a failed marker write may repeat an alert.
