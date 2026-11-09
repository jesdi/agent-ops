# Box status in one call — Spec

## Requirements

1. **Command.** `agent-ops status` is a new noun on the existing `agent-ops` CLI
   (`dispatcher/onboard_cli.py`; the package's only console script). It takes `--config`,
   defaulting like `agent-ops project add` to `config.default_config_path()`, and reads the state
   dir from that config. On the box it is called as `ssh box '~/.local/bin/agent-ops status'`.
2. **Local only.** It makes no network calls: no `gh`, no GitHub API, no usage API. Usage comes
   from the dispatcher's on-disk usage cache, as the console reads it.
3. **Read-only.** It creates, changes or deletes no file under the state dir, takes no
   `convergence.lock`, and runs only reading commands (`systemctl --user` queries,
   `journalctl --user`, herdr tab listing). It never starts, stops, restarts, pauses or unpauses
   anything.
4. **Shared local gates.** The local part of the console's claim forecast
   (`web/read_model.next_claim`) moves into one function that both the console and
   `agent-ops status` call. It returns the first local blocker in this order:
   1. `dispatcher-down`: `pass.json` is missing, or older than 2 × its `interval_minutes`. The
      command alone also counts `agent-ops-dispatcher.timer` not being active (the console,
      polled every second, does not query systemd);
   2. `claims-paused`: `triage-request.json` exists or the herdr `triage` tab is live;
   3. `auth-dark`: the usage gate does not admit the policy default model because usage is
      unknowable (gate reason `unavailable`: no provider answered, usually a dead login);
   4. `budget-blocked`: the usage gate does not admit the policy default model for any other
      reason (the budget is spent and resets on its own);
   5. `projects-paused`: every configured project has `paused: true`;
   6. `capacity-full`: there is no claim room (`claims.claim_room`), or no free slot.
   With none of these it returns "no local blocker". The console's forecast keeps its existing
   verdict names (`dispatcher-down` stays `unknown` there), adds `auth-dark`, and adds
   requirement 5.
5. **Quarantine in the console forecast.** A queue head that has a quarantine record
   (`quarantine/<repository>-<issue>.json`) is not forecast as the next claim; the forecast
   moves to that project's next candidate. The command lists quarantine records but never
   checks whether the blocker is closed.
6. **Verdict line.** The first line is `VERDICT  not claiming: <reason> (<evidence>)` naming the
   first blocker from requirement 4, or `VERDICT  no local blocker — candidates need GitHub (see
   console)`.
7. **Report rows.** Each row follows the verdict, in this order, and reports one source:
   - `dispatcher`: age of the last finished pass, its interval, the dispatcher timer's state.
   - `units`: every `agent-ops-*` user unit or timer in a failed state, with when it failed;
     otherwise `all active`.
   - `projects`: each project with `active` or `PAUSED`.
   - `holds`: triage request (with age), `budget-stalled`, `auth-dark`, and each quarantine
     record as `<repo>#<issue> blocked by <blocker_repo>#<blocker_issue>`; otherwise `none`.
   - `slots`: used / capacity.
   - `tasks`: each task file not done, as `<repo>#<issue>  <stage>  live|<park>[/<reason>]`
     with its age since `updated_at`, and the herdr tab name when the tab is live.
   - `questions`: every task waiting on the operator: each `operator_request` (`plan approval`
     or `answers`) with its page path, age and the console link
     `<console_url>/task/<repository>/<issue>/review`; and every task parked `parked` with
     reason `decision`, with the first 60 characters of its `park_note` and the console link
     `<console_url>/task/<repository>/<issue>`. With `console_url` empty, the link is left out.
     Otherwise `none`.
   - `errors`: the last 20 journal lines from `agent-ops-*` user units in the past 6 hours that
     are priority warning or more severe, or match
     `Traceback|Error|ERROR|Exception|Failed with result`, each with its time and unit;
     otherwise `none`.
8. **Degraded sources.** A source that can't be read (herdr socket gone, `systemctl` or
   `journalctl` failing, a malformed state file) prints `unknown (<why>)` in its row, and the
   report carries on. The exit code is 0 whenever the report was printed, whatever the verdict.
9. **AGENTS.md.** The repo root gets `AGENTS.md` and a `CLAUDE.md` that contains only
   `@AGENTS.md`. `AGENTS.md` says:
   - when the box misbehaves, run `ssh box '~/.local/bin/agent-ops status'` first, then
     diagnose with `/diagnosing-bugs`;
   - a short map of the box: the `box` SSH alias (`agent@ubuntu-4gb-hel1-1`), the state dir
     `~/agent-ops-state` and its files (`targets.yaml`, `pass.json`, `task-*.json`,
     `quarantine/`, `triage-request.json`, `budget-stalled`, `auth-dark`), user units under
     `systemctl --user`, and live sessions as herdr tabs `task-<repo>-<issue>`;
   - probes stay read-only, and an agent asks the operator before restarting or changing
     anything, because restarting `agent-ops-herdr.service` kills every live session.

## Scenarios

The fixture is a state dir under a temp folder with a `targets.yaml` of two projects,
`agent-ops` and `billing`, capacity 3, and fake `systemctl`, `journalctl` and herdr sources. The
clock is 2026-11-06 12:00 UTC.

### Scenario: paused project (Nov 4–6 case)

- **Given** `billing` has `paused: true` and `agent-ops` has `paused: true`, `pass.json` finished
  at 11:57 with `interval_minutes: 5`, and the dispatcher timer is active
- **When** the operator runs `agent-ops status`
- **Then** the first line is `VERDICT  not claiming: projects-paused (agent-ops, billing paused in targets.yaml)`
- **And** the `projects` row shows `agent-ops: PAUSED · billing: PAUSED`

### Scenario: one project paused is not a blocker

- **Given** `billing` is paused and `agent-ops` is not, with a fresh pass and free capacity
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `no local blocker — candidates need GitHub (see console)`
- **And** the `projects` row shows `agent-ops: active · billing: PAUSED`

### Scenario: parked session (Nov 4–6 case)

- **Given** `task-agent-ops-270.json` has stage `review`, park `awaiting-ci`, `updated_at`
  10:00, and `task-agent-ops-281.json` has stage `implement`, no park, and a live herdr tab
  `task-agent-ops-281`
- **When** the operator runs `agent-ops status`
- **Then** the `tasks` row lists `agent-ops#270  review  awaiting-ci  2h` and
  `agent-ops#281  implement  live  task-agent-ops-281`
- **And** the `slots` row shows `1/3`

### Scenario: dispatcher down by stale heartbeat

- **Given** `pass.json` finished at 11:49 with `interval_minutes: 5` (11 minutes, over 2 × 5)
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `not claiming: dispatcher-down (last pass 11m ago, interval 5m)`

### Scenario: heartbeat exactly at the boundary is fresh

- **Given** `pass.json` finished at 11:50 with `interval_minutes: 5` (exactly 2 × 5)
- **When** the operator runs `agent-ops status`
- **Then** the verdict is not `dispatcher-down`

### Scenario: dispatcher down by failed timer

- **Given** a fresh `pass.json` and `agent-ops-dispatcher.timer` reported `failed`
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `not claiming: dispatcher-down (agent-ops-dispatcher.timer failed)`
- **And** the `units` row lists `agent-ops-dispatcher.timer` as failed

### Scenario: dispatcher down by missing heartbeat

- **Given** no `pass.json`
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `not claiming: dispatcher-down (no pass.json)`

### Scenario: gate order — triage beats budget and paused projects

- **Given** `triage-request.json` written at 11:46, the usage gate not admitting, and both
  projects paused
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `not claiming: claims-paused (triage-request.json, 14m old)`
- **And** the `holds` row still lists the triage request

### Scenario: auth dark

- **Given** the usage cache reports every provider `unavailable`, an `auth-dark` marker with
  `since` 11:20, and no triage hold
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `not claiming: auth-dark (usage unknowable since 11:20 — re-login likely)`
- **And** the `holds` row lists `auth-dark since 11:20`

### Scenario: auth dark is not shown as budget blocked in the console

- **Given** the same usage cache, a fresh pass and a non-empty queue
- **When** the console builds its claim forecast
- **Then** the verdict is `auth-dark`, not `budget-blocked`, and the next-claim header shows
  its own auth-dark line

### Scenario: auth dark without a marker yet

- **Given** the usage cache reports every provider `unavailable` and no `auth-dark` marker
  (the dispatcher has not run a pass since)
- **When** the operator runs `agent-ops status`
- **Then** the verdict is still `auth-dark`, with the evidence `usage unknowable`

### Scenario: budget blocked

- **Given** the usage cache's gate does not admit the policy default model, with 40 minutes to
  reset, and no triage hold or auth-dark marker
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `not claiming: budget-blocked (resets in 40m)`

### Scenario: capacity full

- **Given** three in-flight tasks with no park, and capacity 3
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `not claiming: capacity-full (3/3 slots)`

### Scenario: parked tasks free their slot

- **Given** three in-flight tasks, two of them parked `parked`, and capacity 3
- **When** the operator runs `agent-ops status`
- **Then** the verdict is not `capacity-full`, and `slots` shows `1/3`

### Scenario: parked-login keeps its slot

- **Given** three in-flight tasks, one parked `parked-login`, and capacity 3
- **When** the operator runs `agent-ops status`
- **Then** the verdict is `capacity-full`

### Scenario: quarantine records are listed, not resolved

- **Given** `quarantine/agent-ops-212.json` with blocker `agent-ops#205`
- **When** the operator runs `agent-ops status`
- **Then** the `holds` row shows `quarantine: agent-ops#212 blocked by agent-ops#205`
- **And** no `gh` call is made

### Scenario: console skips a quarantined queue head

- **Given** project `agent-ops`'s queue has candidates `#212` then `#213`, a quarantine record
  for `agent-ops#212`, a fresh pass and free capacity
- **When** the console builds its claim forecast
- **Then** the forecast is `will-claim` `agent-ops#213`

### Scenario: console keeps its existing verdicts

- **Given** each local state of the scenarios above, fed to the console's claim forecast with a
  non-empty queue
- **When** the console builds its claim forecast
- **Then** its verdict matches the command's blocker, except that `dispatcher-down` keeps the
  console's existing name `unknown`. `auth-dark` is a new console verdict with its own line in
  the next-claim header. `no-candidates` and `will-claim` still come from the queue part.

### Scenario: questions row

- **Given** `console_url: https://box.example.ts.net`, task `agent-ops#281` with an
  `operator_request` of kind `plan-approval` at `.agent/review.html` parked since 10:00, and
  task `billing#12` parked `parked` with reason `decision` and note
  `Which currency should the invoice default to when the customer has none set?`, since 08:00
- **When** the operator runs `agent-ops status`
- **Then** the `questions` row lists
  `agent-ops#281  plan approval  .agent/review.html  2h  https://box.example.ts.net/task/agent-ops/281/review`
  and `billing#12  decision  "Which currency should the invoice default to when the custo…"  4h  https://box.example.ts.net/task/billing/12`

### Scenario: questions row without a console URL

- **Given** `console_url` is empty and the same `agent-ops#281` request
- **When** the operator runs `agent-ops status`
- **Then** the row lists `agent-ops#281  plan approval  .agent/review.html  2h` with no link

### Scenario: other human parks are not questions

- **Given** a task parked `parked` with reason `stalled`
- **When** the operator runs `agent-ops status`
- **Then** it appears in `tasks` and not in `questions`

### Scenario: error lines

- **Given** the journal holds, within the past 6 hours, 25 matching lines from `agent-ops-*`
  units (a mix of priority-err lines and info-priority `Traceback` lines), one info line
  `claimed agent-ops#290`, and a priority-err line from 7 hours ago
- **When** the operator runs `agent-ops status`
- **Then** the `errors` row shows the last 20 matching lines, each with its time and unit
- **And** it shows neither the `claimed` line nor the 7-hour-old line

### Scenario: a dead source degrades one row

- **Given** the herdr socket is missing and `journalctl` exits non-zero
- **When** the operator runs `agent-ops status`
- **Then** the `tasks` row marks liveness `unknown (herdr socket missing)` and the `errors` row
  reads `unknown (journalctl failed: <its stderr>)`
- **And** every other row and the verdict are still printed, and the exit code is 0

### Scenario: malformed task file

- **Given** `task-agent-ops-299.json` contains `{not json`
- **When** the operator runs `agent-ops status`
- **Then** the `tasks` row shows `agent-ops#299  unknown (unreadable task file)` and the
  report carries on with exit code 0

### Scenario: nothing on the box changes

- **Given** the fixture state dir from the paused-project and parked-session scenarios, with
  every file's content and mtime recorded
- **When** the operator runs `agent-ops status`
- **Then** no file under the state dir was created, changed or deleted
- **And** no fake `systemctl` call was anything other than a query, and `convergence.lock` was
  not acquired

### Scenario: exit code does not track the verdict

- **Given** the dispatcher-down fixture
- **When** the operator runs `agent-ops status`
- **Then** the exit code is 0

### Scenario: AGENTS.md points agents at the command

- **Given** the repo root
- **When** an agent session starts in it
- **Then** `CLAUDE.md` contains exactly `@AGENTS.md`, and `AGENTS.md` names
  `ssh box '~/.local/bin/agent-ops status'`, `/diagnosing-bugs`, the state-dir files of
  requirement 9, and the warning that restarting `agent-ops-herdr.service` kills every live
  session

## Out of scope

- Network checks: the queue, whether a quarantine blocker is closed, live usage.
- `--json`, a health-check exit code, and any command that changes the box.
- Fixing PATH for non-interactive SSH on the box.
- agent-ops-infra's pointer `AGENTS.md`/`CLAUDE.md` (separate infra issue).
