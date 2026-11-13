# 0020. Explicit setup cleanup is timeout-only

Date: 2026-11-13

## Context

A completed setup run already has podman --rm behavior. Host timeout is the path that kills the client without ensuring container removal.

## Decision

Catch TimeoutExpired in workspace setup execution, attempt removal, then re-raise the original exception. Successful setup and non-zero completion trigger no explicit removal.

## Consequences

Provisioning failure reporting keeps its existing exception and quarantine behavior. Cleanup does not change normal setup outcomes.
