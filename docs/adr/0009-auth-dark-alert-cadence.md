# 0009. Auth dark alert cadence

Date: 2026-11-10

## Context

The operator needs reminders during long outages without changing the existing first-alert grace.

## Decision

- **The grace stays 30 minutes and the interval is 8 hours** (operator's answers) — first alert 30 min into an outage, then every 8 h: three alerts over a 21-hour outage.

## Consequences

The first alert is due after 30 minutes and reminders use an 8-hour interval.
