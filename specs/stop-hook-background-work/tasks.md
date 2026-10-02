# Stop hook: don't park a session that is waiting on background work — Tasks

Each task links to the goal in proposal.md: a Claude stage session may end a turn while
background work is running and stay alive, for up to 3 hours, until that work finishes; every
other turn end parks exactly as today. Work the frontier (tasks whose blockers are done), commit
per task with its ID, and finish every task with `make test` green. Tests come first in every
task.

- [ ] **T1** — waitd records a background report. A ping with a non-empty `background_tasks`
  writes the background marker and removes the waiting marker; a ping without it (or with an
  empty list, a non-list, or an empty `target`) is a waiting ping as today and removes the
  background marker. The clock starts on a new marker, restarts when a report names an identity
  the stored marker lacks, and is kept when the report names only stored identities. Identity is
  the entry's `id`, or the whole entry when it has none. The marker is written atomically; an
  unreadable marker reads as absent. `clear_turn_markers` removes both markers. _Goal: the box knows
  a session is waiting on background work and since when._
  Seam: `dispatcher.waitd.handle_ping(body, state_dir)`, read back with
  `dispatcher.state.read_background` / `has_waiting`; clock cases through
  `dispatcher.state.mark_background(..., now=)`. Blocked by: none.
- [ ] **T2** — The Stop hook reports background work. Run as Claude's hook (no argument, hook
  input on stdin) with a `working` stage signal and a non-empty `background_tasks`, it forwards
  the list in its ping. In every other case it sends today's ping: no background work, a
  non-`working` status (`blocked`, `awaiting-answers`, `awaiting-ci`, `awaiting-review`, `done`),
  a missing or corrupt stage signal, empty / `garbage` / `{}` stdin, and Codex's `notify` call
  (JSON argument, stdin not read). It exits 0 in all of them, with waitd up or down. Covers the
  scenarios "turn end with a background gate / agent keeps the session alive" (hook half), "a
  blocked session parks even with background work", "every non-working status…", "missing /
  corrupt stage signal", "unparseable hook input", "waitd down still exits 0" and "Codex turn
  end". _Goal: a turn end with background work stops looking like "waiting for input"._
  Seam: `hooks/stop-hook.sh` as a subprocess against a served waitd. Blocked by: T1.
- [ ] **T3** — The dispatcher honours a background wait. Add `background_wait_seconds` (default
  10800, integer ≥ 1 or load fails) and document it in `targets.example.yaml`. A task with a
  background marker, a live session and a `working` stage signal is not parked and not stalled;
  past the cap it parks for input with the note `(background work still running after 180m — cap
  reached)`, logs `parked` and ends the session. The wait is over once herdr's state-change
  counter moves, and the stall timer applies again; the marker is kept until a waiting ping or
  the session's end, so the same work reported again keeps its clock. A session `working` in
  the foreground is never parked by the cap. A resume after a cap park starts a fresh clock. Add
  **Background wait** to `CONTEXT.md`. Covers "a background wait outlives the stall timer", the
  cap boundary and violation, "new background work restarts the clock", "the same background
  work does not restart the clock", "a resume after a cap park starts a fresh clock", "the cap
  does not park a session that is working", "a woken session that hangs is caught by the stall
  timer" and "the turn end after the background work finished parks as today". _Goal: the
  session stays alive for up to 3 hours, and no longer than that._
  Seam: `dispatcher.main.run_pass(cfg, deps)` with markers placed through
  `dispatcher.state.mark_background(..., now=)`; `dispatcher.config.load_config(path)` for the
  setting. Blocked by: T1.
- [ ] **T4** — Every launch and resume reinstalls the hook. Before the session starts, the
  worktree's `.agent/stop-hook.sh` is the current script and executable, and
  `.claude/settings.local.json` has the current `hooks.Stop` entry with its other keys kept. A
  deleted or modified hook is restored; a dry run installs nothing; `create_workspace` installs
  the same way. Covers "a task claimed before the change gets the new hook on resume" and "a new
  stage launch reinstalls the hook". _Goal: #329, #373 and #293 get the fix on their next
  resume, without a re-claim._
  Seam: `dispatcher.sessions.Sessions.spawn_stage(...)` and `.resume(...)`. Blocked by: none.
