# 0011. Auth dark reminders follow last send

Date: 2026-11-10

## Context

Dispatcher passes may run later than a nominal reminder boundary.

## Decision

- **The interval is measured from the last alert, not from `since`** — alerts land at
  grace + k·interval (0.5 h, 8.5 h, 16.5 h, …) whatever the pass cadence; a late pass delays only that
  reminder, it never shifts later ones earlier.

## Consequences

A late pass moves the next due time to eight hours after the alert it actually sent.
