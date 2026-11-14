# 0018. Setup timeouts have one owner

Date: 2026-11-13

## Context

Separate host and container limits can drift and leave setup running beyond the intended provisioning window.

## Decision

dispatcher/containers.py owns SETUP_TIMEOUT, fixed at 1800 seconds. Both setup_cmd and workspace setup execution read that value. It is code policy, not a configuration option.

## Consequences

A future limit change updates one value and tests both consumers.
