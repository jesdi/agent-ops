# One request reading for console board helpers

## Why

Issue #187 identifies a maintenance hazard in `web/app.py`: board helpers receive the effective configuration alongside separately threaded ordering, usage and time, while some still close over startup pace and capacity. Understanding one response requires tracing two configurations and several argument lists. The current stored settings overlay changes models only, so this is a structural refactor, not evidence that capacity or pace currently changes at runtime. Making one request reading own these inputs removes that ambiguity and prevents later changes from mixing them.

## For whom

Maintainers of the console's board and model-selection code, and operators relying on consistent model and admission information.

## Goal

Build one request-scoped reading and pass it through the board helper family and its existing callers. Every calculation uses that reading's effective configuration, ordering, usage and time, preserving current API behavior and the local-only snapshot.

## Non-goals

No frontend change, API schema change, dispatcher policy change, configuration hot reload, new persistence or cache, transactional snapshot of all source files, or changes to authentication and settings writes. Do not redesign unrelated routes or change the existing `dispatcher.usage.Reading` type.
