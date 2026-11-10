# 0016. Auth dark parses alerted markers

Date: 2026-11-10

## Context

Previously, alerted markers returned before their timestamps were checked.

## Decision

- **An alerted marker is parsed before it returns** — today `alerted` true returns before `since` is read, so a corrupt alerted marker was never repaired; now it is (req 8).

## Consequences

An alerted corrupt marker is repaired instead of silently persisting.
