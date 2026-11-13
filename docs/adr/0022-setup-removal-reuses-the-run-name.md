# 0022. Setup removal reuses the run name

Date: 2026-11-13

## Context

Issue numbers collide across repositories, and the historical task-<issue>-setup name predates repository-scoped identity.

## Decision

Build task-<repository>-<issue>-setup once for a setup attempt and pass that exact name to both setup_cmd and force-removal.

## Consequences

Cleanup stays scoped to the attempted setup container and cannot drift from its launch identity.
