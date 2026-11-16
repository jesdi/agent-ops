# 0019. Unreadable quarantine records fail closed

Date: 2026-11-16

## Context

An unreadable record still represents a task held back after failure. Treating it as absent would reclaim that task repeatedly.

## Decision

`check_quarantine` returns true for an unreadable record, leaves the record in place and makes no blocker lookup. Only Retry or explicit deletion clears that unreadable record.

## Consequences

Unknown contents cannot silently unblock a task. Other candidates can continue through the claim pass.
