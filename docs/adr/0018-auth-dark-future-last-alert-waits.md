# 0018. Auth dark future last alert waits

Date: 2026-11-10

## Context

A clock adjustment can make a stored last_alert appear to be in the future.

## Decision

- **A `last_alert` in the future (clock stepped back) gets no special case** — the reminder is
  delayed until the clock passes it; the box's clock is NTP-synced, so the delay is bounded by
  the step.

## Consequences

The next reminder waits until eight hours have elapsed from that timestamp.
