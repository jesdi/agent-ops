# 0025. Fake API reset is tested through HTTP

Date: 2026-11-16

## Context

Reset affects fixture state and live consumer notifications.

## Decision

Use the real fake API HTTP routes for acceptance tests, including task detail and pending intents for messages. SSE clients have bounded reads and close in cleanup. Existing browser flows provide rendering coverage. Startup equivalence observes a newly launched process before any mutation or reset.

## Consequences

A reset-derived baseline is not a startup oracle. Browser integration coverage remains necessary alongside HTTP tests.
