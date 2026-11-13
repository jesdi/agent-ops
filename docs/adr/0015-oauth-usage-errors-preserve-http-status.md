# 0015. OAuth usage errors preserve HTTP status and Retry-After

Date: 2026-11-13

## Context

A generic transport error hid HTTP 429 from the Anthropic usage adapter. The
adapter needs to distinguish rate limiting from other failures while OpenAI
continues to treat every fetch error as unavailable.

## Decision

`UsageFetchError` carries optional `status` and `retry_after` attributes while
remaining constructible from a message alone. `_http_get_json` catches
`HTTPError` before `URLError` and keeps the HTTP status. It accepts
`Retry-After` only as an ASCII non-negative integer number of seconds; dates,
missing or unusable headers yield `None`. A zero value is preserved for the
adapter to apply its default cooldown. Other transport, timeout and JSON
errors retain no status or retry interval.

## Consequences

The Anthropic adapter can cool down only HTTP 429. Existing callers, including
OpenAI, can keep catching `UsageFetchError` without using the added fields.
