# 0009. Cache failures retain their markers

Date: 2026-11-10

## Context

A cached answer written after a failed `gh` probe must remain visibly stale
or failed on later cache hits. Returning a literal fresh marker from the
cache-hit path would make an outage look like a successful refresh.

## Decision

The rank cache entry stores `stale`: false after a successful fetch and true
after a failed one. Cache hits return that value. A negative-cached issue
description keeps a nonempty `error` in its payload, including when the
exception has no message; no separate description failure field is needed.

## Consequences

- Only a successful fetch clears the failure marker.
- Callers can use the existing response shape to distinguish fresh rank rows
  from stale rows and a failed description from a fetched description.

