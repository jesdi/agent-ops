# 0020. Retry cannot infer a fingerprint from an unreadable record

Date: 2026-11-16

## Context

Retry must work when a quarantine record has no trustworthy readable fingerprint.

## Decision

Retry unlinks an unreadable quarantine record and clears no fingerprint marker from that record. It does not infer a fingerprint from partial contents. Readable-record fingerprint cleanup keeps its existing behavior.

## Consequences

A leftover fingerprint marker can deduplicate the next identical failure report. Repairing or searching for such markers is outside this retry operation.
