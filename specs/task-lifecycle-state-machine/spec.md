# Task lifecycle as one explicit state machine — Spec

## Requirements

1. **Stage and status are separate fields.** `stage` is pipeline position only:
   `queued | spec | plan | implement | review | pr-open | address-review`. `status` is one of
   `running`, `held(spec-approval | login)`, `parked(human | answers(path) | ci(run_id) | review |
   pr | exhausted)`, `waking(from, cause)`, `failed(reason)`, `canceled`, `done`. No transition
   into `failed`, `canceled` or `done` changes `stage`. The stages `awaiting-spec-review`,
   `blocked`, `stalled-on-budget`, `failed`, `done` and `canceled` no longer exist in `Stage`, and
   `TERMINAL_STAGES` / `IN_FLIGHT_STAGES` are replaced by predicates on `status`.
2. **One pure decision function.** `dispatcher/lifecycle.py` exposes
   `decide(task, event) -> Transition(status, stage, actions) | Ignore(reason) | Reject(reason)`.
   The table is declarative rows `(from status kind, event type, named guard, to status, stage
   change, actions)`; guards are named predicates, not inline branches. `decide` does no I/O.
   `dispatcher/machine.py` is folded into it and deleted.
3. **The table is total.** Every (status kind × event type) pair is a declared `Transition`,
   `Ignore` or `Reject` row. A test enumerates every pair and fails on any undeclared one.
4. **Only the transition executor writes `stage` and `status`.** `main.py` executes the actions
   `decide` returns; no other code path saves a changed `stage` or `status`. A test enforces it.
   Every applied `Transition` appends exactly one `transition` eventlog event carrying `from`,
   `event`, `to` and `stage`.
5. **Rejections are loud.** A `Reject` in the dispatcher appends a `transition-rejected` event
   (status, event, reason) and mutates nothing. The web intent endpoints call the same `decide`
   before queueing an intent and answer **409** with the reason on `Reject`. `Ignore` writes only a
   debug log.
6. **The events.** Session: `session-signal`, `session-died`, `session-idle`, `session-waiting`,
   `launch-failed`, `task-crashed`, `grace-expired`, `track-unconfigured`. Operator (web intents
   only): `op-resume`, `op-reply`, `op-kill`, `op-cancel`, `op-park`, `op-login-code`. Scheduler:
   `resume-admitted`, `candidate-ranked`, `flush-due`. PR: `pr-feedback`, `pr-check-failed`,
   `pr-conflict`, `pr-merged`, `pr-closed`. Admission (usage gate, capacity) is evaluated outside
   `decide`; the dispatcher raises `resume-admitted` or a stage spawn only once `admit()` passes.
7. **Capacity and slots follow the status family.** `occupies(status)` is true exactly for
   `running` and `held(*)` and replaces `holds_slot`, `consumes_capacity` and `active`'s rule.
   A transition from a non-occupying into an occupying status emits `AcquireSlot`; the reverse
   emits `ReleaseSlot` (`slot = NO_SLOT`). `waking + resume-admitted` with no free capacity or slot
   stays `waking` and emits `WakeBlocked(reason)` (edge-triggered, as today).
8. **The spec gate is `held`, then `parked`.** A spec session signalling `awaiting-review` moves to
   `stage=spec, held(spec-approval)`. `grace-expired` moves it to `parked(review)` and ends the
   session. `session-died` while `held(spec-approval)` respawns the SPEC stage. A `done` spec
   signal that passes the spec check moves to `stage=plan, running`.
9. **Login is `held(login)` and is answered from the web.** `held(login) + op-login-code` types
   the code into the live pane. The console shows a login-code input on `held(login)` cards.
10. **Resume from `failed` respawns the stage fresh.** `failed(reason) + op-resume` moves to
    `waking(from=failed(reason), cause=resume)`. On `resume-admitted` it respawns `stage` in the same
    worktree on the same ticket, with a prompt carrying the failure reason and the queued messages,
    never `--continue`. Every reason is resumable except `pr-closed`, which is a `Reject` ("reopen
    the PR on GitHub or re-queue the issue"). `killed` is resumable. A task at `pr-open` resumes as
    an `address-review` spawn. `failed(unknown)` whose migrated stage is `queued` is a `Reject`
    ("re-queue the issue instead").
11. **`waking` decides the resume mode.** On `waking + resume-admitted` the table emits exactly one
    of: `ContinueSession(prompt)` when `from` is `parked(human | answers | review | ci | exhausted)`;
    `RespawnStage(stage)` when `from` is `failed(*)` or a `held(spec-approval)` whose session died;
    `SpawnAddressReview` when `stage` is `pr-open`. `cause` is one of
    `reply | resume | attention(kind) | ranked`.
12. **Re-ranking a finished task is declared.** `candidate-ranked` on `failed(r)` with `r ≠
    pr-closed` behaves as `op-resume`. On `failed(pr-closed)` or `canceled` it sweeps the tombstone
    for a fresh claim, keeping today's `pass_started` guard for a tombstone written this pass. On
    `done` it is a `Reject`.
13. **PR lifecycle.** Entering `pr-open` ends the review session and sets `parked(pr)`.
    `parked(pr) + pr-feedback | pr-check-failed | pr-conflict` moves to
    `waking(cause=attention(kind))`, with the loop policy still counting ci rounds; a cap-exhausted
    ci loop moves to `parked(exhausted)`, where only `pr-merged` and `pr-closed` act and the other PR
    events are `Ignore`. `pr-merged` moves to `done`; `pr-closed` moves to `failed(pr-closed)` and
    ends the session. `feedback_pending` and `attention` are removed from `TaskState`.
14. **`operator_request` is derived.** `operator_request(task)` returns `SpecApprovalRequest` for
    `held(spec-approval)`, `AnswersRequest(path)` for `parked(answers(path))`, the value of its
    `from` status for `waking(from, …)`, and `None` otherwise. The stored field and
    `ArmSpecApproval` are removed.
15. **Session signals go through the table.** `running + session-signal` delegates to a stage
    sub-function holding today's `next_actions` rules (artifact checks, plan/spec retries, track
    bounce, `loops.evaluate` → `ApplyDecision`, ticket advance). `stage` changes only there, in the
    claim row, or in a respawn row.
16. **Inbound Telegram is removed.** The `/attach`, `/status`, `/queue`, `/boost`, `/next`,
    reply and plain-text handlers and `hold_for_attach` are deleted. Outbound notifications and the
    morning digest are unchanged.
17. **Old task files migrate lazily.** `state._read` maps every legacy shape to `(stage, status)`
    per the table in the Migration scenarios; the next `save` writes only the new shape.
    `park_note`, `ci_run_id` and the answers path move into the status detail.
18. **The console reads status.** Board columns and zones are unchanged for the operator;
    `column_for(status, stage)` places cards. A failed card shows stage, ticket and reason. The
    actions on a card are the operator events `next_transitions(task)` accepts. `delivery_contract`
    is derived from what `op-reply` does in the current status. The API returns `stage` and
    `status: {kind, detail}` and drops `park`, `feedback_pending`, `hold_for_attach` and
    `operator_request` as stored fields.
19. **The machine can be introspected.** `next_transitions(task)` lists the rows whose `from`
    matches and whose guard passes; `graph()` returns every row as nodes and edges (both status and
    stage progression).
20. **Undecided rows keep today's behaviour.** Any (status, event) behaviour not named above is
    ported unchanged from today's executor, pinned by a characterization test before the move.

## Scenarios

### Scenario: a crash keeps the stage
- **Given** task `portfolio_eval#363` at `stage=implement, running`, `ticket_cursor=2`, `ticket_count=5`
- **When** the pass sees `session-died` with no `done` signal
- **Then** it is `stage=implement, failed(session-crash)`, `ticket_cursor=2`, the slot is `NO_SLOT`,
  and one `transition` event `{from: running, event: session-died, to: failed, stage: implement}` is written

### Scenario: resume after a crash respawns the same ticket
- **Given** `stage=implement, failed(session-crash)`, `ticket_cursor=2`, one queued message "use the v2 API"
- **When** the operator posts Resume
- **Then** the endpoint answers 202, the task becomes `waking(from=failed(session-crash), cause=resume)`,
  and on `resume-admitted` the executor runs `RespawnStage(implement)` for ticket 2 with a prompt
  that contains "session-crash" and "use the v2 API", without `--continue`, and the task is `running` with a slot

### Scenario: resume of a closed-PR failure is refused
- **Given** `stage=pr-open, failed(pr-closed)`
- **When** the operator posts Resume
- **Then** the endpoint answers 409 "reopen the PR on GitHub or re-queue the issue" and no intent file is written

### Scenario: resume of a killed task is allowed
- **Given** `stage=plan, failed(killed)`
- **When** the operator posts Resume
- **Then** the task becomes `waking(from=failed(killed), cause=resume)` and later respawns PLAN

### Scenario: resume of a migrated failure with no known stage is refused
- **Given** a legacy file `{stage: "failed"}` with no `crashed_stage` and `pr_number=0`
- **When** it is read and the operator posts Resume
- **Then** it reads as `stage=queued, failed(unknown)` and the endpoint answers 409 "re-queue the issue instead"

### Scenario: race — intent accepted by the web, rejected by the pass
- **Given** `stage=review, running`; the operator posts Park and the endpoint answers 202
- **When** the pass already in progress moves the task to `stage=pr-open, parked(pr)` before the
  next pass applies the intent
- **Then** `op-park` on `parked(pr)` is a `Reject`, a `transition-rejected` event is written, and the task stays `parked(pr)`

### Scenario: undeclared pair fails CI
- **Given** a new event type `liveness-unknown` added to the enum with no rows
- **When** the test suite runs
- **Then** the totality test fails and names every status kind missing a row for `liveness-unknown`

### Scenario: only the executor writes stage or status
- **Given** a change adding `save(cfg.state_dir, replace(task, stage=Stage.PLAN))` in `_poll_prs`
- **When** the test suite runs
- **Then** the write-ownership test fails naming `_poll_prs`

### Scenario: capacity boundary on resume
- **Given** `capacity=2`, two tasks `running`, one task `waking(from=parked(human), cause=reply)`
- **When** the pass raises `resume-admitted` for the waking task
- **Then** it stays `waking`, a single `wake-blocked` event "capacity full" is written, and a second pass writes no second event

### Scenario: capacity frees and the waking task takes a slot
- **Given** `capacity=2`, one task `running` in slot 0, one `waking` task with `slot=NO_SLOT`
- **When** `resume-admitted` is raised
- **Then** it emits `AcquireSlot`, gets slot 1, becomes `running`, and `occupies` is true for exactly two tasks

### Scenario: parking releases the slot
- **Given** `stage=implement, running` in slot 3
- **When** a `session-signal` with status `blocked` note "which DB?" arrives
- **Then** it is `parked(human)` with note "which DB?", `slot=NO_SLOT`, and `occupies` is false

### Scenario: login hold keeps the slot
- **Given** `stage=spec, running` in slot 1
- **When** the session stops at a `/login` prompt
- **Then** it is `held(login)` and keeps slot 1, and capacity counts it

### Scenario: login code from the console
- **Given** `stage=spec, held(login)`
- **When** the operator submits login code `ABCD-1234` in the console
- **Then** the executor re-verifies the pane is still at a login prompt, types `ABCD-1234`, rewrites
  `stage.json` to `working`, and the task is `stage=spec, running` with the same slot

### Scenario: login code when the pane has left the login prompt
- **Given** `stage=spec, held(login)` whose pane no longer shows a login prompt
- **When** the operator submits a login code
- **Then** nothing is typed, the task stays `held(login)`, and a `transition-rejected` event says
  "no longer at a login prompt — code not typed"

### Scenario: login code on a task that is not held for login
- **Given** `stage=implement, running`
- **When** the operator posts a login code
- **Then** the endpoint answers 409

### Scenario: spec gate holds during grace
- **Given** `stage=spec, running` in slot 0
- **When** the session signals `awaiting-review` with artifact `.agent/spec.md` and a configured track
- **Then** it is `stage=spec, held(spec-approval)`, keeps slot 0, the spec is published, the
  `awaiting_spec_review` notification is sent, and `operator_request(task)` is `SpecApprovalRequest`

### Scenario: spec gate grace expires
- **Given** `stage=spec, held(spec-approval)` in slot 0
- **When** `grace-expired` fires
- **Then** the session ends, it is `parked(review)` with `slot=NO_SLOT`, and `operator_request(task)` is still `SpecApprovalRequest`

### Scenario: spec gate session dies during grace
- **Given** `stage=spec, held(spec-approval)`
- **When** `session-died` fires (e.g. after a reboot)
- **Then** the SPEC stage is respawned in the same worktree and the task is `running`, not failed

### Scenario: spec approval moves to plan
- **Given** `stage=spec, held(spec-approval)`
- **When** the session signals `done` and the spec check passes
- **Then** it is `stage=plan, running` and `operator_request(task)` is `None`

### Scenario: answers request survives a blocked wake
- **Given** `stage=spec, parked(answers(".agent/questions.html"))`
- **When** the operator replies and the wake is blocked on capacity
- **Then** it is `waking(from=parked(answers(...)), cause=reply)` and `operator_request(task)` is
  still `AnswersRequest(".agent/questions.html")` until the resume lands

### Scenario: reply to a parked task continues the transcript
- **Given** `stage=implement, parked(human)`
- **When** the operator replies "yes, Postgres" and the resume is admitted
- **Then** `ContinueSession` resumes the existing session with a prompt containing "yes, Postgres"

### Scenario: failed task re-ranks after the failure issue closes
- **Given** `stage=review, failed(task-crash)` and its card is Ready and no longer blocked
- **When** the claim scan sees it as a candidate (`candidate-ranked`)
- **Then** it becomes `waking(from=failed(task-crash), cause=ranked)`; no second task file or worktree is created

### Scenario: canceled task re-ranks
- **Given** `stage=plan, canceled` written in a previous pass
- **When** `candidate-ranked` fires
- **Then** the tombstone is deleted, a `reopened` event is written, and the issue is claimed fresh at `stage=queued`

### Scenario: canceled this pass does not resurrect
- **Given** a task canceled by an intent in this pass (its `updated_at` ≥ `pass_started`)
- **When** its card still ranks because the board write lagged
- **Then** `candidate-ranked` is `Ignore` and the tombstone stays

### Scenario: done task re-ranks
- **Given** `stage=pr-open, done`
- **When** `candidate-ranked` fires
- **Then** it is a `Reject` and a `transition-rejected` event is written

### Scenario: PR opens and parks on GitHub
- **Given** `stage=review, running` in slot 2
- **When** the session signals `done`
- **Then** it is `stage=pr-open, parked(pr)`, the session is ended, `slot=NO_SLOT`, and `pr_opened` is sent

### Scenario: PR feedback wakes address-review
- **Given** `stage=pr-open, parked(pr)`, `ci_rounds=0`, cap `ci=3`
- **When** `pr-feedback` fires
- **Then** it is `waking(from=parked(pr), cause=attention(feedback))` and on `resume-admitted`
  `SpawnAddressReview` runs and the task is `stage=address-review, running`

### Scenario: ci loop exhausted at pr-open
- **Given** `stage=pr-open, parked(pr)`, `ci_rounds=3`, cap `ci=3`
- **When** `pr-check-failed` fires
- **Then** it is `parked(exhausted)`; a later `pr-conflict` is `Ignore`; a later `pr-merged` moves it to `done`

### Scenario: PR closed unmerged
- **Given** `stage=pr-open, parked(pr)` with PR #88
- **When** `pr-closed` fires
- **Then** it is `stage=pr-open, failed(pr-closed)`, the session is ended, and `pr_closed` is sent

### Scenario: PR merged
- **Given** `stage=pr-open, parked(pr)` with PR #88
- **When** `pr-merged` fires
- **Then** it is `stage=pr-open, done`, `done_at` is set, the worktree and branch are removed, and `task_done` is sent

### Scenario: kill
- **Given** `stage=implement, running`
- **When** the operator posts Kill
- **Then** the session is ended, the card is released to Ready, and it is `stage=implement, failed(killed)`

### Scenario: cancel
- **Given** `stage=spec, parked(human)`
- **When** the operator posts Cancel
- **Then** the board card moves to Wont do, the issue closes as not planned, and it is `stage=spec, canceled`

### Scenario: park intent on a task that is not running
- **Given** `stage=plan, parked(human)`
- **When** the operator posts Park
- **Then** the endpoint answers 409

### Scenario: Telegram inbound is gone
- **Given** the dispatcher runs a pass with a Telegram update "/attach 42" pending
- **When** the pass runs
- **Then** no inbound Telegram handler reads it and task 42 is unchanged; the morning digest still sends at its scheduled time

### Scenario: migration of each legacy shape
- **Given** task files with these shapes
- **When** `state._read` loads them
- **Then** they read as:

| file on disk | `stage` | `status` |
|---|---|---|
| `stage=implement`, `park=""` | implement | `running` |
| `stage=implement`, `park=parked`, `operator_request=null`, `park_note="which DB?"` | implement | `parked(human)` note "which DB?" |
| `stage=spec`, `park=parked`, `operator_request={kind: answers, path: q.html}` | spec | `parked(answers("q.html"))` |
| `stage=awaiting-spec-review`, `park=awaiting-review` | spec | `parked(review)` |
| `stage=awaiting-spec-review`, `park=""` | spec | `held(spec-approval)` |
| `stage=implement`, `park=awaiting-ci`, `ci_run_id=991` | implement | `parked(ci(991))` |
| `stage=spec`, `park=parked-login` | spec | `held(login)` |
| `stage=plan`, `park=unpark-requested` | plan | `waking(from=parked(human), cause=reply)` |
| `stage=pr-open`, `feedback_pending=true`, `attention=conflict` | pr-open | `waking(from=parked(pr), cause=attention(conflict))` |
| `stage=pr-open`, `park=""`, `feedback_pending=false` | pr-open | `parked(pr)` |
| `stage=failed`, `crashed_stage=implement` | implement | `failed(session-crash)` |
| `stage=failed`, no `crashed_stage`, `pr_number=88` | pr-open | `failed(unknown)` |
| `stage=failed`, no `crashed_stage`, `pr_number=0` | queued | `failed(unknown)` |
| `stage=done`, `pr_number=88` | pr-open | `done` |
| `stage=canceled`, `ticket_cursor=0`, `pr_number=0` | queued | `canceled` |
| `stage=blocked`, `ticket_cursor=2` | implement | `parked(human)` note "migrated from legacy blocked stage" |
| `stage=stalled-on-budget`, `ticket_cursor=0`, `pr_number=0` | spec | `parked(human)` note "migrated from legacy stalled-on-budget stage" |
| pre-target legacy `task-42.json` | as above | as above, and the first save retires the legacy file |

### Scenario: migrated file is saved in the new shape
- **Given** a legacy `stage=implement, park=parked` file
- **When** any transition saves it
- **Then** the file has `stage` and `status` and no `park`, `feedback_pending`, `hold_for_attach`, `crashed_stage` or `operator_request` keys

### Scenario: console places cards by status
- **Given** tasks in `held(spec-approval)`, `parked(review)`, `held(login)`, `parked(ci(991))`, `waking(...)`, `parked(pr)`, `failed(session-crash)` at implement ticket 2/5
- **When** the board is built
- **Then** they land in Needs review, Needs review, Parked, Awaiting CI, Resuming, PR open and Failed,
  and the failed card reads "failed at implement · ticket 2/5 · session-crash"

### Scenario: card actions come from the table
- **Given** `stage=pr-open, failed(pr-closed)`
- **When** the task detail is built
- **Then** its actions exclude Resume and include Cancel

### Scenario: next transitions and graph
- **Given** `stage=implement, parked(human)`
- **When** `next_transitions(task)` is called
- **Then** it includes `op-reply → waking`, `op-resume → waking`, `op-kill → failed`, `op-cancel → canceled`,
  and excludes `op-park`; and every row it returns is also an edge in `graph()`

## Out of scope

- Liveness "unknown ≠ dead" (`liveness-unknown`), failure-fingerprint dedupe after a resume, the
  graph/DAG console view, inbound Telegram, changes to admission or loop-round accounting, a
  one-shot migration script, and reading new files with old code. See `proposal.md` Non-goals.
