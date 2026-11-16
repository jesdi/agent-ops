# 0021. QuarantineEntry requires explicit readability

Date: 2026-11-16

## Context

The API previously could only omit corrupt records or return blank rows indistinguishable from readable ones.

## Decision

`QuarantineEntry.readable` is a required boolean with no default. Every producer and fixture states it, and the generated frontend API type carries it.

## Consequences

Consumers branch on the explicit flag. Backend and frontend response contracts must be deployed together; no persisted quarantine format migration is required.
