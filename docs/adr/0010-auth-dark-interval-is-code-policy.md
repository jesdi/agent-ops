# 0010. Auth dark interval is code policy

Date: 2026-11-10

## Context

The alert cadence is a fixed operational policy rather than a project setting.

## Decision

- **`AUTH_DARK_REALERT_HOURS` is a module constant in `dispatcher/main.py`** next to
  `AUTH_DARK_GRACE_MINUTES` — not configurable (proposal non-goal); changing it is a code change.

## Consequences

Changing the reminder interval requires a code change and tests.
