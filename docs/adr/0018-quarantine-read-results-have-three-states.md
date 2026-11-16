# 0018. Quarantine reads distinguish gone, unreadable and readable

Date: 2026-11-16

## Context

A record deleted during a scan must not leave a phantom blocking row; a record that cannot be decoded must not crash a claim pass.

## Decision

The reader reports gone on FileNotFoundError, unreadable on other OSError or ValueError reads, non-object JSON, or wrong-typed known fields, and otherwise returns the object. Known integer fields require exact integers, rejecting booleans, floats and numeric strings; known string fields require strings. Missing fields and extra keys are allowed; an empty object remains readable. This follows the operator decision of 2026-11-16 and retains ADRs 0010 (body-before-filename) and 0011 (reject-boolean-quarantine-issue-fields).

## Consequences

Gone records block nothing and are omitted. Malformed objects share the unreadable state rather than being silently omitted. This supersedes the skip behavior in ADR 0009 (skip-malformed-quarantine-records).
