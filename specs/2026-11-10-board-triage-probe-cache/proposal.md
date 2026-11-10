# Cache the herdr triage probe behind /api/board

## Why

`GET /api/board` calls `Sources.triage_state()` on every request, uncached. That runs
`triage.running()`, which calls `herdr.Tab.find("triage")`: one `tab list` plus one `pane list`,
each a subprocess with `herdr.TIMEOUT = 30`. A wedged herdr server can therefore hold a board
request for up to ~60 s. `board` is a sync `def`, so it runs on the anyio threadpool, and enough
connected clients starve the pool and stall unrelated routes. (The issue's original wording
said "tmux has-session"; `dfddb7a` moved the probe onto herdr. The latency problem remains and is
now two subprocesses, not one.) The degrade direction is already right — a failed probe gives
`False`, so the console never falsely claims claims are paused — so this change is about latency
only. The dispatcher's pass cadence is minutes, so a few seconds of staleness is harmless.

## For whom

The operator using the web console (board view), and every other console route that shares the
threadpool with `/api/board`.

## Goal

A board request spawns the herdr triage probe at most once per `TRIAGE_TTL_SECONDS` (5 s) per
`Sources` instance, including when the probe fails, while a new triage request file still shows
on the very next board load.

## Non-goals

- Threading, background probes, or making `board` async (PR #79 showed concurrency here made
  things worse).
- Having the dispatcher write the triage flag to state (the alternative from the original issue).
- Changing `herdr.TIMEOUT`, `Tab.find`, or `triage.running` semantics.
- Caching `rank_rows`, `issue_description`, or other board data.
- Any change to `web/app.py`, `dispatcher/triage.py` or `dispatcher/herdr.py`.
