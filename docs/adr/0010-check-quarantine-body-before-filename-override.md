# 0010. Check a quarantine body before the filename overrides its key

Date: 2026-11-10

## Context

The filename supplies a quarantine record's task key, even when the body
contains a different `task_issue`. That override can hide a wrong-typed
body field if validation happens afterward.

## Decision

Validate the body as written before replacing `task_issue` with the issue
from the filename. A corrupt body remains malformed even when the filename
supplies a valid key.

## Consequences

The filename remains authoritative for the key of a valid record. Invalid
body types cannot pass validation because an override masks them.
