# Services section — Spec

This change spans two repos: the console lives in `agent-ops`, and the config file and the
timers generated from it live in `agent-ops-infra`.

Terms used below:

- **Job**: a recurring unit of work on the box, run by a timer.
- **Managed job**: an agent-ops job that runs as the `agent` user. Today there are eight:
  dispatcher pass, deploy sync, onboarding pass, Claude login keepalive, Codex login keepalive,
  stale worktree sweep, daily digest, backlog triage.
- **Read-only job**: a job that runs as root. Today there are five: secret cache refresh and
  the four tracker jobs (deploy check, reminders, scanner, backup).
- **Services config**: one file in `agent-ops-infra` holding, per managed job, its schedule and
  whether it is paused.
- **Recording pull request**: a pull request on `agent-ops-infra` that the console opens to
  change the services config and that merges by itself once its checks pass.

## Requirements

1. The console navigation has a "Services" entry that opens the Services page.
2. The page lists every managed job and every read-only job, in two groups: managed jobs first,
   read-only jobs second. Always-on daemons, one-off units and Ubuntu's own timers are not
   listed.
3. Within the managed group, jobs that are running, have a change being applied or carry a
   warning come before paused jobs, and paused jobs come before the rest.
4. Selecting a job shows its detail panel: name, what it does, state (active, paused or
   running), schedule, last run with its duration, next run, the next upcoming run times, and
   its last five runs with the result and duration of each.
5. A read-only job shows its name, what it does, state, schedule, last run with its duration,
   next run and the next upcoming run times, but no list of recent runs. It has no actions and
   no schedule editor, and is marked read-only.
6. Pause on a managed job stops its future runs on the box at once. Resume restores them at
   once. Each is then recorded in the services config through a recording pull request.
7. A pause or resume made from the console holds on the box while its recording pull request is
   still open: the deploy sync does not undo it.
8. Pausing the dispatcher pass, the deploy sync, the Claude login keepalive or the Codex login
   keepalive first asks for confirmation and states the consequence. Declining leaves the job
   unchanged.
9. Run now starts a managed job at once, outside its schedule. A paused job can be run this
   way and stays paused afterwards.
10. Stop run is offered only while a managed job is running. It ends the run in progress, and
    the run is listed as stopped by the operator.
11. The operator can change a managed job's schedule to "every N minutes" (N a whole number of
    at least 1), "daily at HH:MM" in a named timezone, or a custom systemd calendar expression
    such as `Mon..Fri *-*-* 09:00`. A custom expression that systemd does not accept is
    refused before any pull request is opened.
12. A schedule change does not take effect on the box until its recording pull request has
    merged and the deploy sync has applied it. Until then the job is shown as "applying
    change" with the pull request's number, and it keeps running on its old schedule.
13. A recording pull request merges without any approval from the operator.
14. The timers of managed jobs on the box are generated from the services config, so the
    config is the only place a managed job's schedule and paused state are defined.
15. The board's "next pass" countdown follows the dispatcher pass's schedule in the services
    config. There is no second place to change.
16. A managed job whose state on the box differs from the services config, and not because of
    a change made from the console, carries a warning that says so.
17. While a job shows "applying change", its schedule editor is locked: no second schedule
    change can be saved until the first has applied or failed.
18. When a recording pull request cannot merge (its checks fail, it conflicts, or GitHub cannot
    be reached), the job shows "change failed" with a link to the pull request when one
    exists, and offers Retry. A failed schedule change leaves the old schedule running. A
    failed recording of a pause or resume leaves the pause or resume in place on the box.
    Nothing is retried automatically.
19. Stop run never asks for confirmation, on any job.
20. On rollout the services config matches the box as it is: every managed job keeps its
    current schedule, backlog triage is paused, and the other seven are not paused.
21. While the deploy sync is paused, saving a schedule change is refused with a message saying
    the deploy sync is paused, and no recording pull request is opened. Pause, Resume, Run now
    and Stop run still work.

## Scenarios

### Scenario: The page lists managed and read-only jobs

- **Given** the box runs the eight managed jobs and the five read-only jobs, plus
  `agent-ops-herdr.service`, `prototypes.service` and `logrotate.timer`
- **When** the operator opens Services from the console navigation
- **Then** the managed group lists 8 jobs and the read-only group lists 5 jobs
- **And** `agent-ops-herdr`, `prototypes` and `logrotate` appear in neither group

### Scenario: Jobs needing a look come first

- **Given** the stale worktree sweep's timer was switched off on the box by hand, the daily
  digest and backlog triage are paused from the console with nothing left to record, and the
  other five managed jobs are active
- **When** the operator opens the page
- **Then** the stale worktree sweep is first, the daily digest and backlog triage follow, and
  the five active jobs come last

### Scenario: Detail of a frequent job

- **Given** the dispatcher pass runs every 5 minutes, last ran at 10:08 UTC for 27 seconds, and
  it is 10:12 UTC
- **When** the operator selects it
- **Then** the panel shows state "active", schedule "every 5 min", last run "10:08 · 27 s",
  next run "in 1 min", the next three run times, and its last five runs with result and
  duration

### Scenario: Detail of a job that never ran

- **Given** backlog triage is paused and has no recorded run
- **When** the operator selects it
- **Then** the panel shows state "paused", last run "never ran", next run "—", and "No runs
  recorded"

### Scenario: A root job is read-only

- **Given** the secret cache refresh runs as root at 00:00 and 12:00 UTC
- **When** the operator selects it
- **Then** the panel shows its schedule, last run and next run, marked "read-only"
- **And** it shows no list of recent runs
- **And** it has no Pause, Run now, Stop run or schedule editor

### Scenario: Pause takes effect at once and is recorded

- **Given** the daily digest is active with its next run at 08:00 UTC
- **When** the operator presses Pause at 07:55 UTC
- **Then** the job shows "paused" at once and does not run at 08:00
- **And** a recording pull request sets the daily digest to paused in the services config

### Scenario: Resume takes effect at once and is recorded

- **Given** the daily digest is paused
- **When** the operator presses Resume at 07:55 UTC
- **Then** the job shows "active" at once and runs at 08:00
- **And** a recording pull request sets the daily digest to not paused in the services config

### Scenario: The deploy sync does not undo a pause that is still being recorded

- **Given** the operator paused the daily digest and its recording pull request is still open,
  so the services config on the box still says not paused
- **When** the deploy sync runs
- **Then** the daily digest is still paused

### Scenario: Pausing a critical job asks first

- **Given** the Claude login keepalive is active
- **When** the operator presses Pause
- **Then** a confirmation states "The Claude login expires if this stays paused."
- **And** on confirming, the job is paused

### Scenario: Declining the confirmation changes nothing

- **Given** the deploy sync is active
- **When** the operator presses Pause and declines the confirmation
- **Then** the deploy sync is still active and no recording pull request is opened

### Scenario: Pausing a non-critical job does not ask

- **Given** the stale worktree sweep is active
- **When** the operator presses Pause
- **Then** the job is paused with no confirmation

### Scenario: Run now on an active job

- **Given** the stale worktree sweep is active, scheduled daily at 03:00 UTC, and it is 10:12
- **When** the operator presses Run now
- **Then** the job shows "running", and when it ends its last run is 10:12
- **And** its next run is still 03:00

### Scenario: Run now on a paused job

- **Given** backlog triage is paused
- **When** the operator presses Run now
- **Then** the job runs once and is still paused afterwards

### Scenario: Stop a run in progress

- **Given** the dispatcher pass is running
- **When** the operator presses Stop run
- **Then** the run ends with no confirmation asked, and the newest entry in its runs is marked
  stopped by the operator
- **And** the job is still active on its schedule

### Scenario: Stop run is not offered when nothing is running

- **Given** the daily digest is active and not running
- **When** the operator selects it
- **Then** the panel offers Run now and Pause, and no Stop run

### Scenario: Change an interval

- **Given** the dispatcher pass runs every 5 minutes
- **When** the operator saves "every 10 minutes"
- **Then** recording pull request #N is opened, the job shows "applying change · PR #N", and
  it still runs every 5 minutes
- **And** once #N has merged and the deploy sync has run, the job runs every 10 minutes and
  no longer shows "applying change"

### Scenario: Change a daily time with a timezone

- **Given** backlog triage is scheduled daily at 04:00 Europe/Madrid
- **When** the operator saves "daily at 05:00 Europe/Madrid"
- **Then** once the change has applied, the job's next run is 05:00 Europe/Madrid

### Scenario: An interval below 1 minute is refused

- **Given** the onboarding pass runs every 1 minute
- **When** the operator saves "every 0 minutes"
- **Then** the change is refused with a message, and no recording pull request is opened

### Scenario: A recording pull request needs no approval

- **Given** the operator saved a schedule change and recording pull request #N is open
- **When** its checks pass
- **Then** #N merges with no action from the operator

### Scenario: Timers follow the services config

- **Given** the services config says the daily digest runs daily at 09:00 UTC
- **When** the deploy sync runs
- **Then** the daily digest's timer on the box fires at 09:00 UTC

### Scenario: A timer edited by hand is put back

- **Given** the services config says the stale worktree sweep runs daily at 03:00 UTC, and
  someone edits its timer on the box to 06:00
- **When** the deploy sync runs
- **Then** the timer fires at 03:00 UTC again

### Scenario: The board countdown follows the dispatcher schedule

- **Given** the dispatcher pass was changed to every 10 minutes and the change has applied
- **When** a pass finishes at 10:00 UTC
- **Then** the board shows the next pass at 10:10

### Scenario: A difference made outside the console is flagged

- **Given** the services config says the stale worktree sweep is not paused, its timer is
  switched off on the box, and no console change is being recorded
- **When** the operator opens the page
- **Then** the stale worktree sweep shows "paused" with a warning that the box differs from
  the config

### Scenario: A console change still being recorded is not flagged

- **Given** the operator paused the daily digest and its recording pull request is still open
- **When** the operator opens the page
- **Then** the daily digest shows "paused" with no such warning

### Scenario: Save a custom expression

- **Given** the daily digest is scheduled daily at 08:00 UTC
- **When** the operator saves the custom expression `Mon..Fri *-*-* 09:00`
- **Then** a recording pull request is opened, and once it has applied the job's next run is
  the next weekday at 09:00 UTC

### Scenario: An invalid custom expression is refused

- **Given** the daily digest is scheduled daily at 08:00 UTC
- **When** the operator saves the custom expression `every tuesday-ish`
- **Then** the change is refused with a message, no recording pull request is opened, and the
  schedule is still daily at 08:00 UTC

### Scenario: The editor is locked while a change is applying

- **Given** the dispatcher pass shows "applying change · PR #N" for "every 10 minutes"
- **When** the operator selects it
- **Then** the schedule editor cannot be saved
- **And** Pause, Run now and Stop run still work

### Scenario: The editor unlocks when the change has applied

- **Given** the dispatcher pass's change to "every 10 minutes" has merged and been applied
- **When** the operator selects it
- **Then** the schedule editor can be saved again

### Scenario: A schedule change whose pull request fails its checks

- **Given** the operator saved "every 10 minutes" for the dispatcher pass, which ran every 5
  minutes, and recording pull request #N fails its checks
- **When** the operator opens the page
- **Then** the job shows "change failed" with a link to #N and a Retry action
- **And** the job still runs every 5 minutes, and the schedule editor is unlocked

### Scenario: A pause whose recording fails stays in place

- **Given** GitHub cannot be reached
- **When** the operator pauses the daily digest
- **Then** the job is paused on the box at once and shows "change failed" with a Retry action
- **And** the deploy sync does not resume it

### Scenario: Retry records the change

- **Given** the daily digest is paused on the box and shows "change failed", and GitHub can be
  reached again
- **When** the operator presses Retry
- **Then** a recording pull request sets the daily digest to paused in the services config, and
  "change failed" is gone once it merges

### Scenario: A failed change is not retried automatically

- **Given** the dispatcher pass shows "change failed" for pull request #N
- **When** 30 minutes pass with no action from the operator
- **Then** no further recording pull request has been opened for it

### Scenario: The services config on rollout matches the box

- **Given** the box before rollout: dispatcher pass every 5 minutes, deploy sync and onboarding
  pass every 1 minute, both login keepalives every 2 minutes, stale worktree sweep daily at
  03:00 UTC, daily digest daily at 08:00 UTC, and backlog triage switched off with a schedule
  of 04:00 Europe/Madrid
- **When** the change is rolled out
- **Then** every job runs on the same schedule as before, backlog triage shows "paused" with
  no warning, and the other seven show "active"

### Scenario: A schedule save while the deploy sync is paused is refused

- **Given** the deploy sync is paused and the dispatcher pass runs every 5 minutes
- **When** the operator saves "every 10 minutes" for the dispatcher pass
- **Then** the change is refused with a message saying the deploy sync is paused
- **And** no recording pull request is opened and the dispatcher pass still runs every 5
  minutes

### Scenario: Pause still works while the deploy sync is paused

- **Given** the deploy sync is paused and the daily digest is active
- **When** the operator presses Pause on the daily digest
- **Then** the daily digest shows "paused" at once and a recording pull request is opened

## Out of scope

- Creating new jobs from the console.
- One-off scripts, always-on daemons and Ubuntu's own timers.
- Managing root jobs, and managing the tracker's or any other project's jobs.
- Timed pause.
- Timeline and grouped-list layouts.
- Cleaning up units added to the box by hand.
