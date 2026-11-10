# 0013. Auth dark legacy marker uses since

Date: 2026-11-10

## Context

A deployment can encounter a marker written before last_alert existed.

## Decision

- **A legacy marker (`alerted` true, no `last_alert`) reads `last_alert` as `since`** — a box
  mid-incident at deploy re-alerts within at most one interval instead of alerting at once or
  never; costs one branch.

## Consequences

Existing incidents gain reminders without an immediate deployment-time alert.
