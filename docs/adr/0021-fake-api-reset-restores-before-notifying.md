# 0021. Fake API reset restores before notifying

Date: 2026-11-16

## Context

Connected consumers observe board and usage through SSE invalidations.

## Decision

Restore all cards, ghosts, capacity, next claim, priority, messages and intents before pushing changed domains board and usage. Static provider, gate, queue, history and failure fixture values remain unchanged.

## Consequences

A consumer reacting to reset sees a fully restored fixture through the HTTP API.
