# 0014. Auth dark grace ignores stray last alert

Date: 2026-11-10

## Context

An unalerted marker can contain a stale or malformed last_alert field.

## Decision

- **`alerted` false ignores any `last_alert`** — the grace path decides the first alert; a stray
  field cannot skip the grace.

## Consequences

The first alert still follows the incident start and grace period.
