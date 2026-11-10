# 0012. Auth dark reports incident age

Date: 2026-11-10

## Context

The latest reminder may be the only alert the operator sees.

## Decision

- **`minutes` stays the age of `since`** — every reminder says how long the box has been dark,
  so a reader who saw only the latest message still sees the whole outage.

## Consequences

Alert payloads continue to report the age of the entire incident.
