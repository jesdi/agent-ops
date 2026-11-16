# 0022. Fake API reset preserves SSE clients

Date: 2026-11-16

## Context

SSE subscribers are live transport resources, not fixture data.

## Decision

Keep the live subscriber set outside resettable state. A reset retains existing connections and notifies them after restoration.

## Consequences

Reset cannot disconnect consumers or require tests to subscribe again.
