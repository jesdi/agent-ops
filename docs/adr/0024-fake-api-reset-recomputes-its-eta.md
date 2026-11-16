# 0024. Fake API reset recomputes its ETA

Date: 2026-11-16

## Context

A copied startup ETA gets stale during a shared server run.

## Decision

Each reset computes next_pass_eta from the reset clock plus six minutes. Tests bound it by timestamps immediately before and after reset and compare against a measurably earlier ETA.

## Consequences

All other board and usage fields can be compared directly to startup; tests need not wait six minutes.
