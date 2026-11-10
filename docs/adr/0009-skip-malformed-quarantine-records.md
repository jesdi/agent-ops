# 0009. Skip malformed quarantine records as unreadable records

Date: 2026-11-10

## Context

The source already skips unreadable and non-object quarantine records. A
readable object with an invalid field must have a defined result for the
console's failures, board and retry routes.

## Decision

Skip a malformed record using the same `continue` path as an unreadable
record. Add no new state in this change. Issue #87 may later surface both
cases together.

## Consequences

The failures list and board omit the record; retry treats its issue as not
quarantined. A later change to expose skipped records must handle malformed
and unreadable records together.
