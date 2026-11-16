# 0022. Unreadable quarantine rows keep typed blank metadata

Date: 2026-11-16

## Context

Changing every quarantine metadata field to optional would spread nullable handling through the API and its consumers.

## Decision

An unreadable API entry keeps existing field types with blank strings and zero integers, filename-derived identity and `readable: false`. Its `blocker_open` is null and no blocker lookup runs. Consumers use readability rather than guessing from blank metadata.

## Consequences

The existing zero-blocker guard supplies the no-lookup behavior. Unparseable unreadable filenames are omitted because they identify no blocked task.
