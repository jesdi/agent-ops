# Usage cache write failure degrades instead of 500ing

## Why

`fetch_provider` (`dispatcher/usage_providers.py`) fetches a provider's
reading inside a guarded `try`, then writes the per-provider cache
`usage/<provider>.json` with `write_json_atomic` *outside* that guard. Any
`OSError` from that write (full disk, a permissions problem, `usage/` not
being a directory) propagates out of `fetch_provider`. Since PR #79 the
usage view is on `/api/board`'s critical path (`web/app.py` `board()` →
`_usages()` → `Sources.usage()` → `fetch_provider`), so a broken cache file
turns a perfectly good reading into a 500 for the whole board. The same
escape reaches the dispatcher pass and triage through `fetch_all`
(`dispatcher/main.py`, `dispatcher/triage.py`). The issue text names the
pre-refactor `fetch_usage` / `usage-cache.json`; the triage comment on the
issue confirms the bug moved with the code into `fetch_provider` and is
still live. Reproduced on the branch: a plain file at `<state_dir>/usage`
makes `GET /api/board` return 500
(`tests/test_web_sources.py::test_board_survives_an_unwritable_usage_cache`).

## For whom

The operator watching the console board, and the dispatcher pass, which
both depend on usage readings and must never fail because the cache cannot
be written.

## Goal

A failure writing the usage cache costs only the cache: `fetch_provider`
returns the reading it just fetched, logs the write failure, and neither
`/api/board` nor the dispatcher pass sees an exception.

## Non-goals

- Changing how the cache is read (`_cached` already treats an unreadable
  file as a miss).
- Changing `write_json_atomic` itself or its temp-file cleanup.
- Alerting the operator about the cache failure beyond a log line.
- Guarding exceptions other than `OSError` around the write.
