# Park on herdr liveness; poke a stopped session instead of parking it — Spec

Terms:

- **Agent status** is what herdr reports for the session's pane: `working`, `idle`, `blocked`,
  `done`, `unknown`, or none. `idle_seconds` is 0 while the status is `working` and otherwise
  counts from the last status change; it is never 0 for a status other than `working`.
- The **stall timeout** is `stall_after_seconds`, one value for both runtimes; 0 disables stalls
  and pokes.
- A **waiting marker** is the file waitd writes on a turn-end ping that reports no background
  work. A **background marker** is the file it writes on a ping that does.
- A **stopped session** is a live session with a `working` stage signal, a waiting marker, and an
  agent status other than `working`.
- A stopped session is **at a prompt** when its agent status is `blocked` (a permission or trust
  prompt) or its pane shows a login prompt. Text and Enter typed there can approve an action
  without the operator. The dispatcher checks this right before it types the poke, with a new
  read of the agent status and of the pane, not with the idle time of the start of the pass.
- The **pass interval** is `pass_interval_minutes`, paired with the dispatcher's systemd timer;
  5 minutes.
- A **poke** is one line of text typed into the session's pane, followed by Enter, asking the
  agent to check its background work and continue, or to report through the stage signal if it
  needs the operator.
- The **poke cap** is `background_wait_seconds`.

## Requirements

1. **A working session is never parked mid-stage.** While herdr reports `working`, a live
   session with a `working` stage signal is not parked and not poked, whether or not a waiting
   marker exists, however old the marker is, and whichever runtime wrote it.
2. **A stopped session is poked, not parked.** On every pass that finds a stopped session, the
   dispatcher sends one poke and records the time of the first poke. The pass logs the poke. The
   cadence is therefore the pass interval, 5 minutes for both runtimes; a pass that finds the
   session `working` (answering the previous poke, or back at work) sends nothing. Right before
   the text is typed the dispatcher reads the agent status and the pane once more, and that read
   has four outcomes:
   - the status or the pane cannot be read: nothing is typed and nothing changes on the task;
     the next pass tries again;
   - the status is `working` (the session started a turn in the meantime): nothing is typed and
     the first-poke time is cleared;
   - the session is at a prompt: nothing is typed, and on that pass the task parks for the
     operator with the note "(session stopped mid-stage waiting for input)" and the session is
     ended; when the pane shows a login prompt the park is the login park, which keeps the
     session, as today;
   - otherwise the poke is typed.
3. **Poking stops at the cap.** When a pass finds a stopped session whose first poke is older
   than the poke cap, the task parks for the operator with a note that names the cap and the
   number of pokes, a `parked` event is logged, and the session is ended. The cap clock starts at
   the first poke attempt, so a pane that refuses the text is capped too; only delivered pokes
   are counted and logged. A pass that finds the session `working` clears the first-poke time,
   so a session that keeps doing real work is never capped; a session that only answers pokes
   keeps its clock. The cap is checked before the prompt, so a session past the cap parks with
   the cap note also when it is at a prompt.
4. **A hang still parks on the stall timer.** A live session with a `working` stage signal, no
   waiting marker, and an idle time past the stall timeout parks with today's stall note. It is
   never poked: a session that never ended a turn is in a prompt or hung, and typed text would
   not help.
5. **The stage signal still wins.** A `blocked` or `awaiting-answers` signal parks on the pass
   that sees it, poked or not. A `done` or `awaiting-review` signal is handled as today.
6. **The background wait is unchanged.** A live `working` session with a background marker is
   held, not poked, not stalled and not parked, until the state-change counter moves or the cap
   passes, as the stop-hook-background-work spec says.
7. **One timeout for both runtimes.** `codex_stall_after_seconds` is removed; `stall_after_seconds`
   applies to Claude and Codex alike. A config that still sets the Codex key loads with a warning
   naming the key and ignores it.
8. **The pass runs every 5 minutes.** `pass_interval_minutes` defaults to 5 and the dispatcher's
   systemd timer in agent-ops-infra matches it. Nothing else reads the interval differently.
9. **Everything else is unchanged.** Parks from the plan review gate, loop caps and dead
   sessions, and `stall_after_seconds: 0`, behave as today. Waitd, the Stop hook, and Codex
   `notify` are not changed.

## Scenarios

### Scenario: a long foreground review is not parked

- **Given** portfolio_eval #314 in `review` with stage signal `{"stage": "review", "status":
  "working"}`, a live Claude session 40 minutes into a foreground `codex exec` tool call, and
  herdr reporting `working`
- **When** a dispatcher pass runs, with or without a waiting marker for the task
- **Then** the task is not parked and nothing is typed into its pane

### Scenario: a foreign Codex notify does not park the main turn

- **Given** a Codex-runtime task in `implement`, stage signal `working`, a live session herdr
  reports as `working`
- **When** Codex `notify` fires for a title-generation thread, waitd writes a waiting marker, and
  a dispatcher pass runs
- **Then** the task is not parked and not poked

### Scenario: a stale marker from the previous turn does not park the new turn

- **Given** a Claude session that ended turn A, leaving a waiting marker, then started turn B on
  operator input and is `working`
- **When** a dispatcher pass runs
- **Then** the task is not parked and not poked

### Scenario: a Codex turn end on a finished background shell is poked and continues

- **Given** a Codex-runtime task with stage signal `working`, a live session that yielded a gate
  run to the background, ended its turn (waiting marker), and is `done` to herdr while the gate
  finished
- **When** a dispatcher pass runs
- **Then** the task is not parked, one poke is typed into the pane, a `poked` event is logged,
  and the session starts a turn, reads the gate's result and continues the stage

### Scenario: a poke is repeated every pass while the work runs

- **Given** a stopped Codex session poked on the previous pass 5 minutes ago, which answered
  "still running" in a short turn and is `done` again
- **When** a dispatcher pass runs
- **Then** one more poke is typed and the task is not parked

### Scenario: a session answering a poke is not poked again

- **Given** a stopped session poked on the previous pass, now `working` on that turn
- **When** a dispatcher pass runs
- **Then** nothing is typed and the task is not parked

### Scenario: a poked session that needs the operator parks through the stage signal

- **Given** a stopped session that was poked and answered by writing stage signal
  `{"stage": "implement", "status": "blocked", "note": "need the staging credentials"}`
- **When** a dispatcher pass runs
- **Then** the task is parked with the note "need the staging credentials" and the session is
  ended

### Scenario: poking past the cap parks

- **Given** `background_wait_seconds: 10800` and a stopped session first poked 3 hours and 1
  minute ago, stopped again, and not seen `working` by any pass since
- **When** a dispatcher pass runs
- **Then** the task is parked, its note names the 3 hour cap and the number of pokes, a
  `parked` event is logged, and the session is ended

### Scenario: a pane that refuses the poke is still capped

- **Given** `background_wait_seconds: 10800` and a stopped session whose pane has refused every
  poke since the first attempt 3 hours and 1 minute ago
- **When** a dispatcher pass runs
- **Then** the task is parked with the note "(poked 0 times over 180m — stopped mid-stage
  without a stage signal)" and the session is ended

### Scenario: a stopped session at a permission prompt is parked, not poked

- **Given** a Claude session that ended turn A, leaving a waiting marker, then started turn B,
  stopped at a permission prompt, and is `blocked` to herdr; stage signal `working`
- **When** a dispatcher pass runs
- **Then** nothing is typed into the pane, the task is parked with the note "(session stopped
  mid-stage waiting for input)", and the session is ended

### Scenario: a stopped session at a login prompt takes the login park

- **Given** a stopped session whose pane shows a login prompt
- **When** a dispatcher pass runs
- **Then** nothing is typed into the pane, the task takes the login park with the note
  "(session stopped mid-stage waiting for input)", and the session stays alive

### Scenario: herdr cannot be asked at the moment of the poke

- **Given** a stopped session first poked 1 hour ago, and a pass in which the idle time is
  known but the agent status, or the pane, cannot be read right before the poke
- **When** that dispatcher pass runs
- **Then** nothing is typed into the pane, the task is not parked, and its first-poke time and
  poke count are unchanged

### Scenario: the session started a turn between the idle read and the poke

- **Given** a stopped session first poked 1 hour ago that starts a new turn after the pass read
  its idle time, so that herdr reports `working` right before the poke
- **When** that dispatcher pass runs
- **Then** nothing is typed into the pane, the task is not parked, and its first-poke time is
  cleared

### Scenario: real work resets the poke clock

- **Given** a session first poked 2 hours 50 minutes ago that a later pass saw `working`
- **When** it ends a turn again and a pass finds it stopped 20 minutes later
- **Then** it is poked and not parked, and its first-poke time is that pass

### Scenario: a hang parks on the stall timer without a poke

- **Given** `stall_after_seconds: 600`, stage signal `working`, no waiting marker, and a live
  session `idle` to herdr for 11 minutes
- **When** a dispatcher pass runs
- **Then** the task is parked with the note "(no session output for 10m — likely login/trust
  prompt or hang)", nothing is typed into the pane, and the session is ended

### Scenario: a trust prompt parks as today

- **Given** `stall_after_seconds: 600`, no waiting marker, and a live session `blocked` to herdr
  for 11 minutes
- **When** a dispatcher pass runs
- **Then** the task is parked with the stall note and the session is ended

### Scenario: herdr unavailable holds

- **Given** a live `working` session with a waiting marker and `idle_seconds` unknown (herdr
  cannot be asked)
- **When** a dispatcher pass runs
- **Then** the task is not parked and not poked

### Scenario: the background wait still holds a stopped Claude session without poking

- **Given** a live Claude session that ended its turn with `background_tasks` naming
  `make crap-gate`, stage signal `working`, and herdr reporting `idle` for 2 hours
- **When** a dispatcher pass runs
- **Then** the task is not parked and not poked

### Scenario: stall detection off never parks or pokes mid-stage

- **Given** `stall_after_seconds: 0`, stage signal `working`, a waiting marker, and a live
  session `idle` for 3 hours
- **When** a dispatcher pass runs
- **Then** the task is not parked and not poked

### Scenario: a blocked signal parks regardless of liveness

- **Given** stage signal `{"stage": "review", "status": "blocked", "note": "gate cannot run"}`
  and a live session herdr reports as `working`
- **When** a dispatcher pass runs
- **Then** the task is parked with the note "gate cannot run"

### Scenario: the Codex timeout key is ignored with a warning

- **Given** a `targets.yaml` with `stall_after_seconds: 600` and `codex_stall_after_seconds: 1800`
- **When** the config loads
- **Then** it loads, warns that `codex_stall_after_seconds` is no longer read, and a hung Codex
  session (no waiting marker) parks after 10 minutes, not 30

### Scenario: a Codex session inside the container is detected

- **Given** a Codex stage session launched by the dispatcher on the box (tab created with
  `HERDR_AGENT=codex`), 2 minutes into a foreground command
- **When** `herdr agent get` is asked for its pane
- **Then** it answers `codex` with status `working`

## Out of scope

- Changes to waitd, the Stop hook, Codex `notify`, or the marker files.
- Background-work inventory for either runtime.
- A separate poke interval or poke cap setting. The cadence is the pass interval.
- Fixing herdr's status detection.
- Auto-resuming parked tasks.
