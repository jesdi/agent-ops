# Task lifecycle as one explicit state machine — Spec

Re-baselined on 2026-11-10 from the 2026-09-24 draft (issue #132). Requirements 3, 4, 5, 7, 9, 12
and 19 are unchanged from that draft. The others were updated for what `main` became
since: the plan review gate (ADR 0001), one implement session (ADR 0003), one task file format
(ADR 0004), park reasons, pokes, background waits and the access wake.

## Requirements

1. **Stage and status are separate fields.** `stage` is pipeline position only:
   `queued | spec | plan | implement | review | pr-open | address-review`. `status` is one of
   - `running`
   - `held(plan-approval(page, fingerprint) | login)`
   - `parked(human(reason) | answers(path, fingerprint) | access | ci(run_id) |
     review(page, fingerprint) | pr)`
   - `waking(from, cause)`
   - `failed(reason)`, `canceled`, `done`

   `reason` of `parked(human)` is one of `decision | stalled | loop-cap | bad-signal | config |
   operator` and carries the note shown to the operator. No transition into `failed`, `canceled`
   or `done` changes `stage`. The stages `awaiting-plan-review`, `blocked`, `stalled-on-budget`,
   `failed`, `done` and `canceled` no longer exist in `Stage`, and `TERMINAL_STAGES` /
   `IN_FLIGHT_STAGES` are replaced by predicates on `status`.
2. **One pure decision function.** `dispatcher/lifecycle.py` exposes
   `decide(task, event) -> Transition(status, stage, actions) | Ignore(reason) | Reject(reason)`.
   The table is declarative rows `(from status kind, event type, named guard, to status, stage
   change, actions)`; guards are named predicates, not inline branches. `decide` does no I/O.
   `dispatcher/machine.py` (`next_actions` and `pass_actions`) is folded into it and deleted.
3. **The table is total.** Every (status kind × event type) pair is a declared `Transition`,
   `Ignore` or `Reject` row. A test enumerates every pair and fails on any undeclared one.
4. **Only the transition executor writes `stage` and `status`.** `main.py` executes the actions
   `decide` returns; no other code path saves a changed `stage` or `status`. A test enforces it.
   Every applied `Transition` appends exactly one `transition` eventlog event carrying `from`,
   `event`, `to` and `stage`.
5. **Rejections are loud.** A `Reject` in the dispatcher appends a `transition-rejected` event
   (status, event, reason) and mutates nothing. The web intent endpoints call the same `decide`
   before queueing an intent and answer **409** with the reason on `Reject`. `Ignore` writes only a
   debug log. A reply's message is queued before `decide` runs: a rejected `op-reply` rejects the
   wake and keeps the message.
6. **The events.**
   - Session: `session-signal`, `session-died`, `session-idle`, `session-waiting`,
     `launch-failed`, `task-crashed`, `grace-expired`, `track-unconfigured`.
   - Operator (web intents only): `op-resume`, `op-reply`, `op-answers`, `op-kill`, `op-cancel`,
     `op-park`, `op-login-code`.
   - Scheduler: `resume-admitted`, `candidate-ranked`, `flush-due`, `ci-finished`,
     `access-reached`.
   - PR: `pr-feedback`, `pr-check-failed`, `pr-conflict`, `pr-merged`, `pr-closed`.

   Admission (usage gate, capacity) is evaluated outside `decide`; the dispatcher raises
   `resume-admitted` or a stage spawn only once `admit()` passes. The `retry` intent only clears
   quarantine files, changes no task, and stays outside the table.
7. **Capacity and slots follow the status family.** `occupies(status)` is true exactly for
   `running` and `held(*)` and replaces `holds_slot`, `consumes_capacity` and `active`'s rule.
   A transition from a non-occupying into an occupying status emits `AcquireSlot`; the reverse
   emits `ReleaseSlot` (`slot = NO_SLOT`). `waking + resume-admitted` with no free capacity or slot
   stays `waking` and emits `WakeBlocked(reason)` (edge-triggered, as today).
8. **The plan gate is `held`, then `parked`.** The dispatcher alone decides whether the gate
   applies (ADR 0001; the task facts `gated` and `asked` stay on the task).
   - A plan session's ready report that passes the plan check and does not skip the gate moves to
     `stage=plan, held(plan-approval(page, fingerprint))`, publishes the spec and notifies.
   - `grace-expired` moves it to `parked(review(page, fingerprint))` and ends the session.
   - `session-died` while `held(plan-approval)` respawns the PLAN stage while unattended rounds
     remain (`loops.py` keeps the count); with none left it moves to `parked(review)` with a note
     that the session stopped.
   - `op-reply` or `op-answers` while `held(plan-approval)` ends the session, passes through
     `parked(review)` and moves to `waking(cause=reply)`, as today.
   - A `done` signal from the gate session whose approval is armed for the plan revision on disk
     and whose ticket check passes moves to `stage=implement`. A `done` signal with no armed
     approval is bounced back to the session, as today.
   - A ready report that skips the gate moves straight to `stage=implement`.

   The branch for a task that "lost" its gate (`stage=plan` with `gated` set) is deleted: a respawn
   cannot change the status family.
9. **Login is `held(login)` and is answered from the web.** `held(login) + op-login-code` types
   the code into the live pane. The console shows a login-code input on `held(login)` cards.
10. **Resume from `failed` respawns the stage fresh.** `failed(reason) + op-resume` moves to
    `waking(from=failed(reason), cause=resume)`. On `resume-admitted` it respawns `stage` in the same
    worktree, with a prompt carrying the failure reason and the queued messages, never
    `--continue`. Implement is one session (ADR 0003): a respawned implement session picks its
    progress up from its own ledger in `.agent/`, and the dispatcher names no ticket. Every reason
    is resumable except `pr-closed`, which is a `Reject` ("reopen the PR on GitHub or re-queue the
    issue"). `killed` is resumable. A task at `pr-open` resumes as an `address-review` spawn.
    `failed(unknown)` whose migrated stage is `queued` is a `Reject` ("re-queue the issue
    instead").
11. **`waking` decides the launch mode.** On `waking + resume-admitted` the table emits exactly one
    of:
    - `ContinueSession(prompt)` when `from` is `parked(human | answers | review | ci | access)`;
    - `RespawnStage(stage)` when `from` is `failed(*)`, or `cause` is `launch` or `next-stage`;
    - `SpawnAddressReview` when `stage` is `pr-open`.

    `cause` is one of `reply | resume | attention(kind) | ranked | ci | access | launch |
    next-stage`. Two states that are inferred today become `waking`:
    - a claim whose first launch waits on the GitHub App is `stage=queued,
      waking(from=none, cause=launch)`; `TaskState.unlaunched` is removed;
    - an implement session that reported done is `stage=review, waking(from=none,
      cause=next-stage)`; `state.implement_done()` is removed and `picks` is only about models.

    `waking + op-resume` is a `Transition` that stays `waking` and replaces the model override and
    the usage bypass.
12. **Re-ranking a finished task is declared.** `candidate-ranked` on `failed(r)` with `r ≠
    pr-closed` behaves as `op-resume`. On `failed(pr-closed)` or `canceled` it sweeps the tombstone
    for a fresh claim, keeping today's `pass_started` guard for a tombstone written this pass. On
    `done` it is a `Reject`.
13. **PR lifecycle.** Entering `pr-open` ends the review session and sets `parked(pr)`.
    `parked(pr) + pr-feedback | pr-check-failed | pr-conflict` moves to
    `waking(cause=attention(kind))`, with the loop policy still counting ci rounds; a cap-exhausted
    ci loop moves to `parked(human(loop-cap))`, where only `pr-merged`, `pr-closed` and the
    operator events act and the other PR events are `Ignore`. `op-reply` there starts a fresh
    address-review round with the counters reset, as today. `pr-merged` moves to `done`;
    `pr-closed` moves to `failed(pr-closed)` from every non-terminal status and ends the session.
    `feedback_pending` and `attention` are removed from `TaskState`.
14. **`operator_request` is derived.** `operator_request(task)` returns
    `PlanApprovalRequest(page, fingerprint)` for `held(plan-approval)` and `parked(review)`,
    `AnswersRequest(path, fingerprint)` for `parked(answers)`, the value of its `from` status for
    `waking(from, …)`, and `None` otherwise. The stored field is removed. Arming and disarming an
    approval for a plan revision keep today's rules.
15. **Session signals go through the table.** `running + session-signal` delegates to a stage
    sub-function holding today's `next_actions` rules (artifact checks, plan/spec retries, track
    bounce, `loops.evaluate` → `ApplyDecision`). `stage` changes only there, in the claim row, or
    in a respawn row.
16. **Inbound Telegram is removed.** The `/attach`, `/status`, `/queue`, `/boost`, `/next`,
    reply, plain-text and login-code handlers are deleted, with `hold_for_attach` and
    `park_msg_id`. The console login-code input of requirement 9 ships before the removal.
    Outbound notifications and the morning digest are unchanged. `dispatcher/tmux_migration.py`
    and its test are deleted (the box has no tmux server, checked 2026-11-10). The call to
    `--migrate-tmux` in `agent-ops-infra/provision/update.sh` is removed first, in a pull request
    of that repository.
17. **Old task files are migrated once (ADR 0004).** A migration command rewrites every task file
    to `(stage, status)` per the table in the Migration scenario, with the dispatcher stopped,
    under the pass lock, all-or-nothing, and safe to run twice. `update.sh` of `agent-ops-infra`
    runs it on the box, inside the lock it already holds (`--lock-held`: the command takes no
    lock); run by hand, the command takes the pass lock itself. A woken file
    (`park=unpark-requested`) gets the `from` its other fields give as if it were still parked.
    A file the table does not classify stops the run. Until every file is migrated the dispatcher
    starts no pass. The loader does not convert a retired stage value; it drops retired fields. `park`, `park_reason`, `park_note`, `ci_run_id`,
    `crashed_stage`, `unlaunched` and the stored request move into the status detail.
18. **The console and `agent-ops status` read status.** Board columns and zones are unchanged for
    the operator; `column_for(status, stage)` places cards, and the task page branches on the same
    column. A failed card shows stage and reason. The actions on a card are the operator events
    `next_transitions(task)` accepts. `delivery_contract` is derived from what `op-reply` does in
    the current status. The API returns `stage` and `status: {kind, detail}` and drops `park`,
    `park_reason`, `feedback_pending`, `hold_for_attach` and `operator_request` as stored fields.
    `web/box_status.py` prints the status kind and detail in its tasks and questions rows.
19. **The machine can be introspected.** `next_transitions(task)` lists the rows whose `from`
    matches and whose guard passes; `graph()` returns every row as nodes and edges (both status and
    stage progression).
20. **Undecided rows keep today's behaviour.** Any (status, event) behaviour not named above is
    ported unchanged from today's executor, pinned by a characterization test before the move.
    This covers pokes of a stopped session (`session-idle`) and background waits
    (`session-waiting`): both leave the task `running`, and their caps end in
    `parked(human(stalled))`.
21. **Access parks wake by themselves.** A task whose repository the GitHub App cannot reach is
    `parked(access)`. `access-reached` moves it to `waking(cause=access)`; operator events act on
    it as on `parked(human)`.
22. **It ships as several pull requests**, each leaving `main` deployable:
    1. the closed-PR fix (a parked `pr-open` task whose PR closes ends failed and not parked);
    2. `park` / `unpark` / `end` functions that own today's park fields, with no file change;
    3. `lifecycle.py` with the totality test, and the session-signal path through it;
    4. the status shape and the migration command;
    5. each remaining event family;
    6. the console login-code input, then the Telegram removal;
    7. web and frontend on status;
    8. the write-ownership test, `CONTEXT.md` and README.

## Scenarios

### Scenario: a crash keeps the stage
- **Given** task `portfolio_eval#363` at `stage=implement, running`
- **When** the pass sees `session-died` with no `done` signal
- **Then** it is `stage=implement, failed(session-crash)`, the slot is `NO_SLOT`, and one
  `transition` event `{from: running, event: session-died, to: failed, stage: implement}` is written

### Scenario: resume after a crash respawns the stage
- **Given** `stage=implement, failed(session-crash)`, one queued message "use the v2 API"
- **When** the operator posts Resume
- **Then** the endpoint answers 202, the task becomes `waking(from=failed(session-crash), cause=resume)`,
  and on `resume-admitted` the executor runs `RespawnStage(implement)` with a prompt that contains
  "session-crash" and "use the v2 API", without `--continue`, and the task is `running` with a slot

### Scenario: resume of a closed-PR failure is refused
- **Given** `stage=pr-open, failed(pr-closed)`
- **When** the operator posts Resume
- **Then** the endpoint answers 409 "reopen the PR on GitHub or re-queue the issue" and no intent file is written

### Scenario: resume of a killed task is allowed
- **Given** `stage=plan, failed(killed)`
- **When** the operator posts Resume
- **Then** the task becomes `waking(from=failed(killed), cause=resume)` and later respawns PLAN

### Scenario: resume of a migrated failure with no known stage is refused
- **Given** a migrated task `stage=queued, failed(unknown)`
- **When** the operator posts Resume
- **Then** the endpoint answers 409 "re-queue the issue instead"

### Scenario: resume on a task that is already waking
- **Given** `stage=implement, waking(from=parked(human(decision)), cause=reply)` with no model override
- **When** the operator posts Resume with model `anthropic/claude-opus-5`
- **Then** the endpoint answers 202, the task stays `waking`, and its model override is `anthropic/claude-opus-5`

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
- **Given** `capacity=2`, two tasks `running`, one task `waking(from=parked(human(decision)), cause=reply)`
- **When** the pass raises `resume-admitted` for the waking task
- **Then** it stays `waking`, a single `wake-blocked` event "capacity full" is written, and a second pass writes no second event

### Scenario: capacity frees and the waking task takes a slot
- **Given** `capacity=2`, one task `running` in slot 0, one `waking` task with `slot=NO_SLOT`
- **When** `resume-admitted` is raised
- **Then** it emits `AcquireSlot`, gets slot 1, becomes `running`, and `occupies` is true for exactly two tasks

### Scenario: parking releases the slot
- **Given** `stage=implement, running` in slot 3
- **When** a `session-signal` with status `blocked` note "which DB?" arrives
- **Then** it is `parked(human(decision))` with note "which DB?", `slot=NO_SLOT`, and `occupies` is false

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

### Scenario: plan gate holds during grace
- **Given** `stage=plan, running` in slot 0, on a track that is not gate-free
- **When** the session reports ready with review page `.agent/review.html` and the plan check passes
- **Then** it is `stage=plan, held(plan-approval(".agent/review.html", <fingerprint>))`, keeps slot 0,
  the spec is published, the `awaiting_plan_review` notification is sent, `gated` is true, and
  `operator_request(task)` is a `PlanApprovalRequest` for that page

### Scenario: plan gate is skipped
- **Given** `stage=plan, running` on a gate-free track, `asked` false, `gated` false
- **When** the session reports ready with `open_questions: 0`, the review page counts 0 open
  questions, and the plan and ticket checks pass
- **Then** it is `stage=implement` with no `held` status in between and no operator request

### Scenario: plan gate grace expires
- **Given** `stage=plan, held(plan-approval(page, fp))` in slot 0
- **When** `grace-expired` fires
- **Then** the session ends, it is `parked(review(page, fp))` with `slot=NO_SLOT`, and
  `operator_request(task)` is still the same `PlanApprovalRequest`

### Scenario: plan gate session dies during grace
- **Given** `stage=plan, held(plan-approval(page, fp))` with unattended rounds left
- **When** `session-died` fires (e.g. after a reboot)
- **Then** the PLAN stage is respawned in the same worktree and the task is `stage=plan, running`, not failed

### Scenario: plan gate session dies with no rounds left
- **Given** `stage=plan, held(plan-approval(page, fp))` with no unattended rounds left
- **When** `session-died` fires
- **Then** it is `parked(review(page, fp))` with the note "plan session stopped at the review gate"
  and `plan_session_stopped` is sent

### Scenario: reply while the gate session is alive
- **Given** `stage=plan, held(plan-approval(page, fp))` in slot 2
- **When** the operator replies "split ticket 3"
- **Then** the session is ended, the slot is released, the task is
  `waking(from=parked(review(page, fp)), cause=reply)`, and the resumed session's prompt contains "split ticket 3"

### Scenario: plan approval moves to implement
- **Given** `stage=plan, held(plan-approval(page, fp))` and the operator approved revision `fp`
- **When** the session signals `done`, the plan on disk still has fingerprint `fp`, and the ticket check passes
- **Then** it is `stage=implement` and `operator_request(task)` is `None`

### Scenario: done with no approval is bounced
- **Given** `stage=plan, running`, never at the gate
- **When** the session signals `done`
- **Then** the task stays `stage=plan, running` and the session is told that `done` is accepted only after approval

### Scenario: answers request survives a blocked wake
- **Given** `stage=spec, parked(answers(".agent/questions.html", fp))`
- **When** the operator replies and the wake is blocked on capacity
- **Then** it is `waking(from=parked(answers(...)), cause=reply)` and `operator_request(task)` is
  still `AnswersRequest(".agent/questions.html", fp)` until the resume lands

### Scenario: reply to a parked task continues the transcript
- **Given** `stage=implement, parked(human(decision))`
- **When** the operator replies "yes, Postgres" and the resume is admitted
- **Then** `ContinueSession` resumes the existing session with a prompt containing "yes, Postgres"

### Scenario: access park wakes by itself
- **Given** `stage=queued, parked(access)` for repository `jesdi/portfolio_eval`
- **When** the GitHub App reaches `jesdi/portfolio_eval` and `access-reached` fires
- **Then** it is `waking(from=parked(access), cause=access)` with no operator action

### Scenario: claim that waits on the App
- **Given** a Ready card is claimed while minting its App token fails
- **When** the claim is committed
- **Then** the task is `stage=queued, waking(from=none, cause=launch)`, and on the next
  `resume-admitted` the SPEC stage is spawned and it is `stage=spec, running`

### Scenario: implement done waits for the review launch
- **Given** `stage=implement, running`, `capacity=1`
- **When** the session signals `done` and admission for the review model is denied
- **Then** it is `stage=review, waking(from=none, cause=next-stage)` with `slot=NO_SLOT`, and once
  admitted `RespawnStage(review)` runs and it is `stage=review, running`

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
- **Then** it is `parked(human(loop-cap))`; a later `pr-conflict` is `Ignore`; a later `pr-merged` moves it to `done`

### Scenario: reply to an exhausted PR task
- **Given** `stage=pr-open, parked(human(loop-cap))`, `ci_rounds=3`
- **When** the operator replies "try once more"
- **Then** it is `waking(cause=reply)` with `ci_rounds=0`, and once admitted `SpawnAddressReview`
  runs with a prompt containing "try once more"

### Scenario: PR closed unmerged
- **Given** `stage=pr-open, parked(pr)` with PR #88
- **When** `pr-closed` fires
- **Then** it is `stage=pr-open, failed(pr-closed)`, the session is ended, and `pr_closed` is sent

### Scenario: PR closed while parked for the operator
- **Given** `stage=pr-open, parked(human(loop-cap))` with PR #88
- **When** `pr-closed` fires
- **Then** it is `stage=pr-open, failed(pr-closed)`, its card is in Failed and not in Parked, and a
  later `op-reply` is a `Reject`

### Scenario: PR merged
- **Given** `stage=pr-open, parked(pr)` with PR #88
- **When** `pr-merged` fires
- **Then** it is `stage=pr-open, done`, `done_at` is set, the worktree and branch are removed, and `task_done` is sent

### Scenario: kill
- **Given** `stage=implement, running`
- **When** the operator posts Kill
- **Then** the session is ended, the card is released to Ready, and it is `stage=implement, failed(killed)`

### Scenario: cancel
- **Given** `stage=spec, parked(human(decision))`
- **When** the operator posts Cancel
- **Then** the board card moves to Wont do, the issue closes as not planned, and it is `stage=spec, canceled`

### Scenario: park intent on a task that is not running
- **Given** `stage=plan, parked(human(operator))`
- **When** the operator posts Park
- **Then** the endpoint answers 409

### Scenario: a stopped session is poked, then parked
- **Given** `stage=implement, running`, poke cap 2, a session that stays stopped
- **When** `session-idle` fires on three passes in a row
- **Then** the first two deliver a poke and leave it `running`, and the third moves it to
  `parked(human(stalled))`

### Scenario: Telegram inbound is gone
- **Given** the dispatcher runs a pass with a Telegram update "/attach 42" pending
- **When** the pass runs
- **Then** no inbound Telegram handler reads it and task 42 is unchanged; the morning digest still sends at its scheduled time

### Scenario: migration command rewrites each legacy shape
- **Given** the dispatcher is stopped and the state dir holds task files with these shapes
- **When** the migration command runs
- **Then** every file is rewritten as:

| file on disk | `stage` | `status` |
|---|---|---|
| `stage=implement`, `park=""` | implement | `running` |
| `stage=implement`, `park=parked`, `park_reason=decision`, `park_note="which DB?"` | implement | `parked(human(decision))` note "which DB?" |
| `stage=implement`, `park=parked`, `park_reason=""` | implement | `parked(human(operator))` |
| `stage=queued`, `park=parked`, `park_reason=access` | queued | `parked(access)` |
| `stage=spec`, `park=parked`, `operator_request={kind: answers, path: q.html, fingerprint: ab12}` | spec | `parked(answers("q.html", "ab12"))` |
| `stage=awaiting-plan-review`, `park=awaiting-review`, `operator_request={kind: plan-approval, path: review.html, fingerprint: cd34}` | plan | `parked(review("review.html", "cd34"))` |
| `stage=awaiting-plan-review`, `park=""`, same request | plan | `held(plan-approval("review.html", "cd34"))` |
| `stage=implement`, `park=awaiting-ci`, `ci_run_id=991` | implement | `parked(ci(991))` |
| `stage=spec`, `park=parked-login` | spec | `held(login)` |
| `stage=plan`, `park=unpark-requested` | plan | `waking(from=parked(human(operator)), cause=reply)` |
| `stage=implement`, `park=unpark-requested`, `crashed_stage=implement` | implement | `waking(from=failed(session-crash), cause=resume)` |
| `stage=spec`, `park=unpark-requested`, `operator_request={kind: answers, path: q.html, fingerprint: ab12}` | spec | `waking(from=parked(answers("q.html", "ab12")), cause=reply)` |
| `stage=awaiting-plan-review`, `park=unpark-requested`, `operator_request={kind: plan-approval, path: review.html, fingerprint: cd34}` | plan | `waking(from=parked(review("review.html", "cd34")), cause=reply)` |
| `stage=queued`, `park=unpark-requested`, `park_reason=access` | queued | `waking(from=parked(access), cause=access)` |
| `stage=queued`, `unlaunched=true` | queued | `waking(from=none, cause=launch)` |
| `stage=implement`, `implement_providers=[anthropic]`, no `implement` pick | review | `waking(from=none, cause=next-stage)` |
| `stage=pr-open`, `feedback_pending=true`, `attention=conflict` | pr-open | `waking(from=parked(pr), cause=attention(conflict))` |
| `stage=pr-open`, `park=""`, `feedback_pending=false` | pr-open | `parked(pr)` |
| `stage=pr-open`, `park=parked`, `park_reason="loop cap"` | pr-open | `parked(human(loop-cap))` |
| `stage=failed`, `crashed_stage=implement` | implement | `failed(session-crash)` |
| `stage=failed`, `park=parked`, `pr_number=88` | pr-open | `failed(unknown)` |
| `stage=failed`, no `crashed_stage`, `pr_number=88` | pr-open | `failed(unknown)` |
| `stage=failed`, no `crashed_stage`, `pr_number=0` | queued | `failed(unknown)` |
| `stage=done`, `pr_number=88` | pr-open | `done` |
| `stage=canceled`, `pr_number=0` | queued | `canceled` |
| `stage=blocked` | implement | `parked(human(operator))` note "migrated from legacy blocked stage" |
| `stage=stalled-on-budget`, `pr_number=0` | spec | `parked(human(operator))` note "migrated from legacy stalled-on-budget stage" |

### Scenario: migration is all-or-nothing
- **Given** 13 task files, one of which is not valid JSON
- **When** the migration command runs
- **Then** it exits non-zero naming that file and no task file is changed

### Scenario: migration is safe to run twice
- **Given** a state dir the migration command already rewrote
- **When** it runs again
- **Then** it exits zero and no file's bytes change

### Scenario: migration refuses a running dispatcher
- **Given** a dispatcher pass holds the pass lock
- **When** the migration command runs
- **Then** it exits non-zero with "dispatcher is running" and no task file is changed

### Scenario: migration run by the updater
- **Given** `update.sh` holds the pass lock and the state dir holds legacy task files
- **When** it runs the migration command with `--lock-held`
- **Then** the command takes no lock, every file is rewritten, and it exits zero

### Scenario: an unclassified file stops the migration
- **Given** 13 task files, one with `park=awaiting-ci` and `ci_run_id=0`
- **When** the migration command runs
- **Then** it exits non-zero naming that file and no task file is changed

### Scenario: the dispatcher waits for the migration
- **Given** a state dir with one task file in the old shape
- **When** the dispatcher starts
- **Then** it exits non-zero before its pass, names the migration command, and changes no file

### Scenario: a rejected reply keeps its message
- **Given** `stage=pr-open, parked(human(loop-cap))`; the operator replies "try once more" and the endpoint answers 202
- **When** the pass in progress moves the task to `failed(pr-closed)` before the next pass applies the reply
- **Then** `op-reply` is a `Reject`, a `transition-rejected` event is written, and "try once more" stays in the message queue, undelivered

### Scenario: migrated file has only the new shape
- **Given** a legacy `stage=implement, park=parked` file
- **When** the migration command rewrites it
- **Then** the file has `stage` and `status` and no `park`, `park_reason`, `park_note`, `park_msg_id`,
  `feedback_pending`, `hold_for_attach`, `crashed_stage`, `unlaunched` or `operator_request` keys

### Scenario: console places cards by status
- **Given** tasks in `held(plan-approval)`, `parked(review)`, `held(login)`, `parked(access)`,
  `parked(ci(991))`, `waking(...)`, `parked(pr)`, `failed(session-crash)` at implement
- **When** the board is built
- **Then** they land in Needs review, Needs review, Parked, Parked, Awaiting CI, Resuming, PR open
  and Failed, and the failed card reads "failed at implement · session-crash"

### Scenario: task page and card agree
- **Given** `stage=implement, parked(ci(991))`
- **When** the task page is built
- **Then** it shows no "reply below to wake" panel, matching the card, which is in Awaiting CI

### Scenario: card actions come from the table
- **Given** `stage=pr-open, failed(pr-closed)`
- **When** the task detail is built
- **Then** its actions exclude Resume and include Cancel

### Scenario: agent-ops status prints the status
- **Given** `stage=plan, parked(human(decision))` with note "which DB?"
- **When** `agent-ops status` runs
- **Then** its tasks row shows `parked(human(decision))` and its questions row shows "which DB?"

### Scenario: next transitions and graph
- **Given** `stage=implement, parked(human(decision))`
- **When** `next_transitions(task)` is called
- **Then** it includes `op-reply → waking`, `op-resume → waking`, `op-kill → failed`, `op-cancel → canceled`,
  and excludes `op-park`; and every row it returns is also an edge in `graph()`

## Out of scope

- Liveness "unknown ≠ dead" (`liveness-unknown`), failure-fingerprint dedupe after a resume, the
  graph/DAG console view, changes to admission or loop-round accounting, the box claim state
  (`agent-ops status` VERDICT inputs), and reading new files with old code.
