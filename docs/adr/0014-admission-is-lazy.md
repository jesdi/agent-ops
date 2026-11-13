# 0014. Admission advances only after the caller acts

Date: 2026-11-13

## Context

A spawn changes box-wide capacity and slot ownership. Precomputing admissions would give later candidates a stale view of those resources.

## Decision

Woken and feedback tasks use the lazy `_admissions` generator. Snapshot and sort candidates once when iteration starts, but re-read capacity and slots for each candidate after the previous caller action. Callers must not materialize the generator before spawning.

## Consequences

A spawn that fills the box refuses every later candidate in that call. Tasks that become candidates during the call wait until the next pass.
