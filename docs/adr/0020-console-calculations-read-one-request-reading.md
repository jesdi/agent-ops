# 0020. Console calculations read one request reading

Date: 2026-11-17

## Context

Issue #187: the console's board helpers in `web/app.py` were each handed the effective configuration plus a separate ordering, usage and time, and some still used startup values (`boot.pace`, `boot.capacity`, `boot.max_open`). Following one response meant tracing two configurations and several argument lists. A route could mix two configurations, two clocks or two priority modes in one response, or treat the snapshot's uncollected usage as a fetched empty map.

## Decision

Each route builds one `RequestReading` (`web/app.py`) at its edge with `_reading()`. It holds the effective `Config`, the captured UTC `now`, the priority `mode`, the `order` derived from them, and `usages`. Every calculation helper takes the reading plus domain arguments only (task, repository, row, queues). Helpers never call `_cfg()`, `datetime.now`, `priority.load` or `sources.usage` themselves, and never read pace, capacity, `max_open` or policies from `boot`. A project's policy comes from the reading's own repositories.

`usages is None` means usage was not collected: the local snapshot, or a run that an explicit model, saved pick or pending override decides. Such a reading skips admission. A fetched `{}`, or providers that are all unavailable, is a real reading and fails closed. Only `/api/board`, task detail, both usage routes and an undecided task run fetch usage.

The reading lives for one request. Nothing caches it across requests.

## Consequences

A new board-family helper takes `RequestReading` first. A new route that needs these inputs calls `_reading()` once, never per helper. Tests inject the effective configuration and clock through `create_app(config_loader=..., clock=...)`. Startup maps (`repositories_by_name`, `project_members`) remain only for repository identity and project membership, which targets.yaml fixes at startup. This is not `dispatcher.usage` "Reading", a provider's `ProviderUsage`.
