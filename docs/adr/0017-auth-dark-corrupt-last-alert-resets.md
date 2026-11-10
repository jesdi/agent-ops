# 0017. Auth dark corrupt last alert resets

Date: 2026-11-10

## Context

A malformed last_alert cannot supply a reliable reminder clock.

## Decision

- **An unparseable `last_alert` (on an alerted marker) resets the marker like an unparseable `since`** — one repair
  path; the price is that the incident restarts its grace (≤ 30 min of silence) and `minutes`
  restarts from the repair.

## Consequences

Repair restarts the incident grace and its displayed age.
