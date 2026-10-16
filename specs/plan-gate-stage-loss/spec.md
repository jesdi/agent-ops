# Plan gate: an approved plan must not go back to review — Spec

Terms:

- **At the gate**: the task's stage is `awaiting-plan-review`.
- **Gated**: the task has waited at the gate at least once (`gated: true`). The flag is never
  cleared.
- **Recorded conversation**: the session record (`session-<target>-<issue>`) that waitd writes
  from the Stop hook's ping, and that a resume continues.
- **Fresh plan launch of a gated task**: any launch of a new plan conversation for a gated task:
  a wake with no recorded conversation (or one recorded for another stage), a rejected signal's
  retry with no recorded conversation, the respawn of a dead session at the gate, or the
  operator's Resume of a task that crashed at the gate.
- **Task identity file**: the worktree's `.agent/task.json`, read by the Stop hook and the push
  guardrail.

## Requirements

1. **A plan launch of a gated task runs at the gate stage.** After a fresh plan launch of a gated
   task, the task's stage is `awaiting-plan-review`, the same as after a resume of a recorded
   conversation at the gate. A plan launch for a task that is not gated (from `spec`, or a crash
   Resume of a plan that never reached the gate) sets stage `plan`, as today. So after this change
   a gated task is never in stage `plan`.
2. **An approval reported after a fresh launch starts implement.** When the session of a fresh
   plan launch of a gated task reports `done` with a configured track (or no track) and the ticket
   set passes the check, implement starts on that track, exactly as for a resumed conversation.
3. **A `done` from a gated task in stage `plan` parks for the operator.** Only a task saved
   before this change can be in this state. When a task that is gated and in stage `plan`
   reports `done`, the dispatcher does not tell the session to ask for
   review again. It parks the task for the operator, with a note that says the gate state was
   lost and that the operator can move the task to implement by hand. The note names the
   reported track. A task that is not gated keeps today's bounce (`PLAN_NO_APPROVAL`).
4. **Every launch and resume heals the task identity file.** Before every session launch and
   every resume, the dispatcher writes `.agent/task.json` with the task's `issue`, `target` and
   `branch`, also when the file is missing, malformed, or has an old shape. From then on the Stop
   hook's ping carries the target, and waitd records the conversation.
5. **A healed worktree resumes its conversation.** After a launch that healed the task identity
   file, the next turn end records the conversation, and the next wake of that task continues it
   instead of starting a new one.

## Scenarios

### Scenario: wake at the gate with no recorded conversation keeps the gate stage

- **Given** portfolio_eval #283 at `awaiting-plan-review`, parked for review, track `standard`,
  `gated: true`, and no session record
- **When** the operator submits "approve" and the next dispatcher pass wakes the task
- **Then** a new plan conversation starts (event `resumed`, detail `new conversation: no session
  recorded`), and the saved task's stage is `awaiting-plan-review`

### Scenario: approval after a fresh launch starts implement

- **Given** portfolio_eval #283 at `awaiting-plan-review` after a fresh plan launch from the gate,
  track `standard`, and one ticket under `.agent/tickets/` that passes the check
- **When** the session writes `{"stage": "plan", "status": "done", "track": "standard"}` and a
  dispatcher pass runs
- **Then** an implement session starts with `ticket_count: 1`, the task's track is `standard`,
  and the operator gets `implement_started`, not `awaiting_plan_review`

### Scenario: approval with a new track after a fresh launch

- **Given** the same task, track `standard`, and the track `research` is configured
- **When** the session reports `done` with track `research` and a dispatcher pass runs
- **Then** implement starts and the task's track is `research`

### Scenario: resumed conversation at the gate is unchanged

- **Given** portfolio_eval #226 at `awaiting-plan-review` with a session record for stage `plan`
- **When** the operator approves and the next pass wakes the task
- **Then** the recorded conversation is resumed (event `resumed`, empty detail), and the stage is
  `awaiting-plan-review`

### Scenario: respawn of a dead session at the gate keeps the gate stage

- **Given** a task at `awaiting-plan-review`, not parked, its session dead, with 1 unattended
  round used of 2
- **When** a dispatcher pass runs
- **Then** a new plan session starts, the stage is `awaiting-plan-review`, and the unattended
  rounds are 2

### Scenario: crash Resume of a gated task runs at the gate stage

- **Given** portfolio_eval #283, `gated: true`, track `standard`, that crashed at the gate (stage
  `failed`, `crashed_stage: plan`), and an approval in `.agent/review-answers.json`
- **When** the operator resumes it, the next pass launches it, and the new session reports `done`
  with track `standard` and one ticket that passes the check
- **Then** the launch saves stage `awaiting-plan-review`, `unattended_rounds` is 0 after the
  launch, and implement starts with `ticket_count: 1`

### Scenario: crash Resume of a plan that never reached the gate runs in stage plan

- **Given** a task, `gated: false`, that crashed in stage `plan` (`crashed_stage: plan`)
- **When** the operator resumes it and the next pass launches it
- **Then** the saved stage is `plan`

### Scenario: plan launch from spec still sets stage plan

- **Given** a task in `spec`, never gated
- **When** the spec session reports `done` with a checked `spec.md` and track `standard`, and a
  dispatcher pass runs
- **Then** a plan session starts and the task's stage is `plan`

### Scenario: done from a gated task in stage plan parks with a clear note

- **Given** portfolio_eval #283 in stage `plan`, `gated: true`, a live session
- **When** the session reports `done` with track `standard` and a dispatcher pass runs
- **Then** the task parks for the operator, the session is not resumed with `PLAN_NO_APPROVAL`,
  no `plan_retry` is sent, and the park note says the gate state was lost, names track
  `standard`, and says the task can be moved to implement by hand

### Scenario: done from an ungated task in stage plan still bounces

- **Given** a task in stage `plan`, `gated: false`, `plan_retries: 0`
- **When** the session reports `done` and a dispatcher pass runs
- **Then** the session is resumed with the `PLAN_NO_APPROVAL` reason, as today

### Scenario: old task identity file is healed on resume

- **Given** a worktree whose `.agent/task.json` is `{"issue": 283}`, task portfolio_eval #283,
  branch `agent/task-283`
- **When** the dispatcher resumes or launches a session for the task
- **Then** `.agent/task.json` is `{"issue": 283, "target": "portfolio_eval", "branch":
  "agent/task-283"}` before the session starts

### Scenario: missing or malformed task identity file is healed

- **Given** a worktree whose `.agent/task.json` is missing, or holds `[]`, or holds text that is
  not JSON
- **When** the dispatcher launches a session for the task
- **Then** `.agent/task.json` holds the task's issue, target and branch

### Scenario: healed worktree records and resumes its conversation

- **Given** portfolio_eval #283 whose `.agent/task.json` was `{"issue": 283}`, launched once
  after the heal
- **When** the session ends its turn (the Stop hook pings with session id `abc-123`), the task
  parks, and the operator wakes it
- **Then** the session record holds `abc-123`, and the wake resumes conversation `abc-123`
  instead of starting a new one

### Scenario: a session cannot plant another branch through the identity file

- **Given** a session that wrote `{"issue": 283, "target": "portfolio_eval", "branch": "main"}`
  into `.agent/task.json`
- **When** the dispatcher next launches or resumes the task
- **Then** `.agent/task.json` holds branch `agent/task-283` again

## Out of scope

- Automatic repair of a task that already lost the gate stage (portfolio_eval #283 today).
- The dispatcher reading `review-answers.json` to decide on the approval.
- Removal of waitd's legacy ping path and cleanup of legacy `waiting-<issue>` markers.
