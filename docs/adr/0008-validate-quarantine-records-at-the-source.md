# 0008. Validate quarantine records at the source boundary

Date: 2026-11-10

## Context

`Sources.quarantine_entries()` supplies the failures, board and retry routes.
A readable JSON object can have a wrong-typed field and make a consumer fail.

## Decision

Validate the fields in `Sources.quarantine_entries()` before returning a
record. Every consumer receives the same filtered list. Route-level exception
handling would leave other consumers exposed and could allow an invalid
`blocker_issue` to reach a GitHub lookup.

## Consequences

Future consumers of quarantine entries use this boundary. Changes to the
record's accepted fields belong here rather than in individual routes.
