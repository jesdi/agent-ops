# 0011. Reject booleans in quarantine issue fields

Date: 2026-11-10

## Context

Python treats `bool` as a subclass of `int`, while JSON booleans and integers
are different value types. `isinstance(value, int)` would accept `true` and
`false` for `task_issue` or `blocker_issue`.

## Decision

Require an exact integer type for those fields. Reject booleans, floats and
numeric strings; do not coerce them into issue numbers.

## Consequences

Malformed issue numbers are skipped before a downstream GitHub lookup or
response model receives them. Future validators must preserve the exact type
rule.
