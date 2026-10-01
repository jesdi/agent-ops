# Stop hook: don't park a session that is waiting on background work — Spec

Terms:

- The **stage signal** is the worktree's `.agent/stage.json`.
- A **turn end** is Claude Code firing the Stop hook.
- **Background work** is any entry in the hook input's `background_tasks`: a background shell or
  a background agent the session started and that is still running at the turn end.
- A **background wait** is the time a session spends stopped after a turn end that had background
  work and a `working` stage signal. It ends when the session starts its next turn; from then on
  the session is an ordinary working session again.

## Requirements

1. **A background wait is not a park.** When a Claude session ends a turn with background work
   and a `working` stage signal, the task is not parked, the session stays alive, and the
   10-minute stall timer does not park it.
2. **A background wait is capped.** When a session is in a background wait and the cap has
   passed since it last started new background work, the task parks for the operator with a
   note that names the cap, and the session is ended. The cap is 3 hours by default and is
   configurable. The clock restarts at every turn end that reports background work the previous
   turn end did not report. A turn end where only the same background work is still running does
   not restart it.
3. **Finished background work continues the stage.** When a session's background work finishes
   during a background wait, the session picks up the result and keeps working on the stage,
   with no operator or dispatcher action. The background wait is over: its next turn end is
   judged by these same rules, and the 10-minute stall timer applies to it again as today.
4. **Everything else is unchanged.** A turn end is handled exactly as today when any of these
   holds: there is no background work; the stage signal's status is not `working` (`blocked`,
   `awaiting-answers`, `awaiting-ci`, `awaiting-review`, `done`), with or without background
   work; the stage signal or the hook input is missing or unreadable; the hook input has no
   `background_tasks`; or the hook runs as Codex's `notify` program. The hook always exits 0.
5. **In-flight tasks get the new hook.** Every session launch and every resume installs the
   current hook and hook settings into the task's worktree, so a task claimed before this change
   behaves as specified from its next launch or resume.

## Scenarios

### Scenario: turn end with a background gate keeps the session alive

- **Given** portfolio_eval #329 in `review` with stage signal `{"stage": "review", "status":
  "working"}` and a live session
- **When** the session ends a turn with hook input `{"background_tasks": [{"type": "shell",
  "command": "make crap-gate"}]}`, and a dispatcher pass runs
- **Then** the hook exits 0, the task is not parked, and the session is still alive

### Scenario: turn end with a background agent keeps the session alive

- **Given** the same task and stage signal
- **When** the session ends a turn with one background agent in `background_tasks`, and a
  dispatcher pass runs
- **Then** the hook exits 0, the task is not parked, and the session is still alive

### Scenario: a background wait outlives the stall timer

- **Given** `stall_after_seconds: 600`, and portfolio_eval #329 in a background wait that began
  11 minutes ago, its session idle since then
- **When** a dispatcher pass runs
- **Then** the task is not parked and the session is still alive

### Scenario: background wait at the cap boundary

- **Given** a cap of 3 hours and portfolio_eval #329 in a background wait on `make crap-gate`,
  first reported 2 h 59 min ago
- **When** a dispatcher pass runs
- **Then** the task is not parked

### Scenario: background wait past the cap parks

- **Given** a cap of 3 hours and portfolio_eval #329 in a background wait on `make crap-gate`,
  first reported 3 h 1 min ago
- **When** a dispatcher pass runs
- **Then** the task is parked for the operator, its note says the background work ran past the
  3-hour cap, a `parked` event is logged, and the session is ended

### Scenario: new background work restarts the clock

- **Given** a cap of 3 hours and portfolio_eval #329 whose background `make crap-gate` was first
  reported 2 h 50 min ago and has finished
- **When** the woken session starts the review agents in the background and ends its turn
  reporting them, and a dispatcher pass runs 20 minutes later
- **Then** the task is not parked

### Scenario: the same background work does not restart the clock

- **Given** a cap of 3 hours and portfolio_eval #329 whose background dev server was first
  reported 2 h 50 min ago and is still running
- **When** the session takes a turn and ends it reporting only that same dev server, and a
  dispatcher pass runs 20 minutes later
- **Then** the task is parked with the note naming the 3-hour cap and the session is ended

### Scenario: a resume after a cap park starts a fresh clock

- **Given** portfolio_eval #329 parked for running past the 3-hour cap
- **When** an operator reply resumes it, the new session ends a turn with background work, and a
  dispatcher pass runs 10 minutes later
- **Then** the task is not parked

### Scenario: the cap does not park a session that is working

- **Given** a cap of 3 hours and portfolio_eval #329 whose background gate was first reported
  3 h 30 min ago, and whose session has since been woken and is working in the foreground
- **When** a dispatcher pass runs
- **Then** the task is not parked

### Scenario: a finished background gate continues the stage

- **Given** portfolio_eval #329 in a background wait on `make crap-gate`, with no operator
  attached
- **When** the gate finishes
- **Then** the session starts a new turn on its own with the gate's result, and the task was
  never parked in between

### Scenario: the turn end after the background work finished parks as today

- **Given** portfolio_eval #329 woken by its finished gate, stage signal still `working`
- **When** the session ends that turn with hook input `{"background_tasks": []}`, and a
  dispatcher pass runs
- **Then** the task is parked with the note "(session stopped mid-stage waiting for input)" and
  the session is ended

### Scenario: a woken session that hangs is caught by the stall timer

- **Given** `stall_after_seconds: 600`, a cap of 3 hours, and portfolio_eval #329 woken by its
  finished gate 30 minutes into the cap, which then sits idle for 11 minutes without ending a
  turn
- **When** a dispatcher pass runs
- **Then** the task is parked with today's stall note ("no session output for 10m …") and the
  session is ended

### Scenario: turn end with nothing running parks as today

- **Given** stage signal `{"stage": "review", "status": "working"}` and waitd listening
- **When** the Stop hook runs with hook input `{"background_tasks": []}`, and a dispatcher pass
  runs
- **Then** the hook exits 0, waitd receives one ping, and the task is parked with the note
  "(session stopped mid-stage waiting for input)"

### Scenario: a blocked session parks even with background work

- **Given** stage signal `{"stage": "review", "status": "blocked", "note": "gate cannot run"}`
- **When** the session ends a turn with hook input `{"background_tasks": [{"type": "shell",
  "command": "make crap-gate"}]}`, and a dispatcher pass runs
- **Then** the hook exits 0 and the task is parked with the note "gate cannot run"

### Scenario: every non-working status is handled as today with background work

- **Given** a stage signal whose status is, in turn, `awaiting-answers`, `awaiting-ci`,
  `awaiting-review` and `done`
- **When** the Stop hook runs with one background shell in `background_tasks`
- **Then** each run exits 0 and sends the same ping to waitd as today

### Scenario: missing stage signal is handled as today

- **Given** a worktree with `.agent/task.json` and no `.agent/stage.json`
- **When** the Stop hook runs with one background shell in `background_tasks`
- **Then** it exits 0 and waitd receives one ping

### Scenario: corrupt stage signal is handled as today

- **Given** a worktree whose `.agent/stage.json` contains `{not json`
- **When** the Stop hook runs with one background shell in `background_tasks`
- **Then** it exits 0 and waitd receives one ping

### Scenario: unparseable hook input is handled as today

- **Given** stage signal status `working`
- **When** the Stop hook runs with empty stdin, with stdin `garbage`, and with stdin `{}`
- **Then** all three runs exit 0 and each sends one ping

### Scenario: waitd down still exits 0

- **Given** stage signal status `working` and no waitd socket
- **When** the Stop hook runs, once with one background shell in `background_tasks` and once
  with none
- **Then** both runs exit 0

### Scenario: Codex turn end is handled as today

- **Given** stage signal `{"stage": "implement", "status": "working"}`
- **When** the hook is run the way Codex runs `notify`: one JSON argument
  (`{"type": "agent-turn-complete"}`) and nothing on stdin
- **Then** it exits 0 and waitd receives one ping

### Scenario: a task claimed before the change gets the new hook on resume

- **Given** portfolio_eval #329, parked, whose worktree holds the hook installed on 2026-09-11
- **When** an operator reply resumes it
- **Then** the worktree's hook and hook settings are the current ones before the session starts

### Scenario: a new stage launch reinstalls the hook

- **Given** a task whose worktree hook was modified or deleted during the previous stage
- **When** the dispatcher launches the next stage's session
- **Then** the worktree's hook and hook settings are the current ones before the session starts

## Out of scope

- Any change to a turn end with no background work, including for an attached operator.
- A stage-signal status for "working in the background".
- Auto-resuming parked tasks from the dispatcher.
- Tracking background work for Codex sessions.
- Detecting background work from processes or the screen.
- The stall timer's handling of a session that is not in a background wait.
- Prompt changes.
- Cleaning up stale legacy `waiting-<issue>` markers on the box.
