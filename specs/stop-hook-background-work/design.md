# Stop hook: don't park a session that is waiting on background work — Design

## Decisions

- **The hook reports, waitd records, the dispatcher decides.** The Stop hook adds the hook
  input's `background_tasks` to its existing ping; waitd turns that into a background marker —
  the hook stays a thin shell script and the clock rule lives in testable Python, but a report
  sent while waitd is down is lost and the session falls back to the 10-minute stall park.
- **One ping endpoint, one extra field.** A ping whose body has a non-empty `background_tasks`
  list is a background report; any other ping is a waiting ping, as today — an old waitd ignores
  the field and parks as today, an old hook never sends it, so hook and waitd can deploy in
  either order.
- **The hook sends `background_tasks` only for a Claude turn end with a `working` stage signal.**
  Every "as today" case of requirement 4 is decided in the hook by leaving the field out — waitd
  and the dispatcher need no status logic for it.
- **Claude vs Codex is told by arguments.** No argument means Claude (hook input on stdin); any
  argument means Codex `notify`, and stdin is never read — relies on Codex always passing its
  JSON argument, which it does today.
- **A background task's identity is its `id`, or the whole entry when it has none.** Two entries
  without ids and with identical fields count as the same work, so re-running the same command
  does not restart the clock on a Claude Code version that sends no ids.
- **The latest turn end wins.** A background report deletes the waiting marker and a waiting
  ping deletes the background marker — a task can never be in both states.
- **`clear_waiting` clears both markers.** Every site that ends or replaces a session already
  calls it, so a new session never inherits an old clock — the name now undersells what it does.
- **The wait ends when herdr's state-change counter moves.** The dispatcher records the counter
  on the first pass that sees a report with the agent not `working`; a different counter on a
  later pass means the session started a new turn, the marker is deleted and the stall timer
  applies again — a wake that happens before that first pass is not seen, and that session is
  held until the cap instead of the stall timer (ceiling: one pass interval of exposure).
- **The cap parks only a session that is still in the wait.** A woken session working in the
  foreground is never parked by the cap — a session herdr reports as `working` throughout is
  therefore never capped, exactly as a working session is never stalled today.
- **The cap is `background_wait_seconds` in `targets.yaml`, default 10800.** Seconds, beside
  `stall_after_seconds`, for one unit across both timers — hours would read better.
- **The cap park is an ordinary input park.** It goes through the existing park-for-input path
  with the note `(background work still running after <N>m — cap reached)`, N being the cap in
  whole minutes (180 for the default) — the session and its background work are killed, and the
  operator's reply resumes it with a fresh clock.
- **The hook is reinstalled inside the session launch.** `spawn_stage` and `resume` both go
  through one launch path; installing there covers every launch site at once — the sessions
  module now depends on the workspace installer.
- **Reinstalling rewrites the hook script and only the `hooks.Stop` entry of
  `.claude/settings.local.json`.** Other keys the session accumulated (permission rules) are
  kept — a merge instead of the wholesale write `create_workspace` does today.
- **Requirement 3's wake is Claude Code's own behaviour.** No code implements it; it is checked
  end to end on the box after deploy, not by the unit suite.

## Data model

No database. Files in the dispatcher's state dir and fields on the task.

| Table | Field | Constraint | Why |
|---|---|---|---|
| `background-<target>-<issue>` (marker file, written only by waitd) | `tasks` | non-empty list of identity strings | the work the latest turn end reported; compared with the next report to detect new work |
| | `since` | epoch seconds; set to the report time when the marker is new or the report names an identity not in the stored `tasks`; otherwise unchanged | the cap clock (requirement 2) |
| | `reported` | epoch seconds of the latest report; always `>= since` | tells the dispatcher a new turn end happened |
| | whole file | written atomically (temporary file, then rename); an unreadable marker reads as absent | the dispatcher never sees half a marker |
| `waiting-<target>-<issue>` (existing) | — | never exists together with the background marker | the latest turn end wins |
| `TaskState` | `background_reported` (float, default 0.0) | the marker's `reported` the dispatcher last recorded a counter for | detects a new report |
| | `background_seq` (int, default 0) | herdr's `state_change_seq` at that recording | detects the session starting a new turn |
| `Config` | `background_wait_seconds` (int, default 10800) | integer ≥ 1, otherwise config load fails | the cap |

Background wait, as the dispatcher sees one unparked task with a live session:

| State | Condition | Pass does |
|---|---|---|
| none | no background marker | today's rules |
| reported | marker present, `reported` differs from `background_reported`, agent not `working` | record `reported` and the counter; no park, no stall |
| reported, agent `working` or unknown | as above but herdr says `working` or cannot be asked | nothing this pass |
| waiting | `reported` recorded, counter unchanged, `now - since` ≤ cap | no park, no stall |
| capped | `reported` recorded, counter unchanged, `now - since` > cap | park for input with the cap note |
| over | `reported` recorded, counter changed | delete the marker; today's rules, stall timer included |

A waiting marker or a non-`working` stage signal takes precedence over all of these, as today.

## Identities

None.

## Seams

- `hooks/stop-hook.sh` run as a subprocess against a served waitd, with the hook input on stdin
  (Claude) or as an argument (Codex) — existing; observed through the state functions below.
- `dispatcher.waitd.handle_ping(body: bytes, state_dir) -> None` — existing; new body field
  `background_tasks`.
- `dispatcher.state.read_background(state_dir, target, issue) -> BackgroundWait | None`, where
  `BackgroundWait(tasks: tuple[str, ...], since: float, reported: float)` — new.
- `dispatcher.state.mark_background(state_dir, target, issue, tasks: list, now: float | None =
  None) -> None` — new; `tasks` are raw `background_tasks` entries, `now` lets a test place a
  report in the past.
- `dispatcher.state.has_waiting` / `clear_waiting` — existing.
- `dispatcher.main.run_pass(cfg, deps)` — existing. The sessions fake gains
  `agent_state(target, issue) -> tuple[str, int] | None` (herdr status and state-change counter),
  the new method on `dispatcher.sessions.Sessions`.
- `dispatcher.config.load_config(path)` — existing.
- `dispatcher.sessions.Sessions.spawn_stage(...)` and `.resume(...)` — existing; observed through
  the worktree's `.agent/stop-hook.sh` and `.claude/settings.local.json`.
