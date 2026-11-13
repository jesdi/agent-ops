# 0017. Setup containers have two timeout bounds

Date: 2026-11-13

## Context

Killing the foreground podman client does not stop its setup container. A dispatcher that dies cannot perform host-side cleanup.

## Decision

Bound setup with both podman's run timeout and host-side force-removal when provisioning raises TimeoutExpired. Keep both mechanisms: the podman bound survives dispatcher death, while force-removal frees the name for retry.

## Consequences

Two mechanisms protect different failure paths. Existing orphaned containers still require manual removal; this is not a sweep.
