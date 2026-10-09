# Bound task turns and Codex background results — Spec

## Requirements

1. **A stopped-mid-stage park requires an authoritative current main turn end.** For a live task whose
   stage signal is `working`, a waiting marker alone is insufficient. The turn end must belong
   to the bound main conversation of the current stage or implement ticket and its latest
   main turn. Codex uses bound `turn/started` and normal `turn/completed` control events;
   Claude uses native session/prompt hooks. Native Codex Stop/notify is supplementary.
   A child, internal title thread, independent conversation, old turn, or previous
   launch cannot supply this evidence. Missing or unreadable evidence is not a turn end.
2. **New main input cancels the old waiting marker.** When a new main prompt starts, the
   previous turn's waiting marker no longer applies. A delayed Stop from that old turn cannot
   restore it. Clearing this marker does not remove the background-work cap clock.
3. **Bind control and results to the current launch.** Conversation IDs, main turn IDs, worker
   identities, completion records, and delivery records belong to one target, issue, and fresh
   stage/ticket launch. Resumes name that launch's recorded conversation. Late events cannot
   change another task or a replacement launch, including the next ticket of the same stage.
   Preserve #154's continue-or-restart behavior when no valid conversation record exists.
4. **Owned background work keeps the task running.** After the current main turn stops with a
   `working` stage signal, running task-owned commands or spawned agents keep the task In
   progress, its session alive, and its capacity slot held. The ordinary stall timer does not
   park a confirmed background wait. Agent descendants at any depth count; unrelated or
   internal helper threads do not. Unknown inventory is not empty inventory.
   A command becomes eligible background work only when authoritative inventory confirms
   that it survives the matching normal main Stop. Terminal inventory while its initiating
   main turn is active includes foreground commands and cannot alone prove eligibility.
5. **Codex remains available in the task terminal.** Run its app-server behind the existing
   task terminal, with a control client for the bound conversation. The operator can attach,
   watch results, and submit input through the existing task flows. Control-client recovery
   does not replace the terminal or start another conversation.
6. **Deliver each available completion promptly.** A command exit or agent completion is
   delivered to the main agent without waiting for the other workers. Combine completions
   already available into one delivery. If the main conversation is idle, continue that same
   conversation with the results. Pending completions must be considered before an idle main
   turn is treated as stopped waiting for operator input. Completion delivery must not depend
   on the next 10-minute dispatcher pass. Previously confirmed background commands remain
   eligible for delivery during later active main turns. Commands that start and finish
   before their first qualifying Stop are not independently redelivered: the supported
   protocol cannot distinguish their outcomes from already consumed foreground output.
7. **Deliver into an active main turn without interrupting it.** If a main turn is working,
   send the result into that turn with its exact turn-ID precondition, without a second turn
   or interruption. If the turn changes before acceptance, read current state again and choose
   active delivery or idle continuation from that state. An operator prompt in flight must not
   cause a duplicate main turn or redirect the result to another conversation.
8. **Reconcile uncertain delivery.** Each completion and delivery has a stable identity that
   survives a control-client or dispatcher restart. After a lost acknowledgment, confirm
   acceptance from the bound conversation's history before any retry. A matching accepted
   message marks delivery successful. If receipt remains uncertain, keep the result pending,
   alert the operator, leave the task/session running, and keep checking history; do not resend
   blindly. A duplicate event or history record must not create another delivery. A confirmed
   rejection is distinct from uncertain acceptance.
9. **Reconnect to recover missed work and results.** If only the control client disconnects,
   keep the session and terminal running, reconnect to the exact bound conversation, reconcile
   running work with stored work identities, and recover completions that occurred while the
   client was offline. Reconnection alone changes neither the conversation nor the wait clock.
   Loss of control visibility alone cannot establish a stopped-mid-stage park or successful
   completion of a worker.
10. **Use the existing background-wait cap and clock rules.** Use `background_wait_seconds`,
    default 10800 seconds. Start its clock at the first stopped main-turn report with running
    background work. A later report of genuinely new work resets it; a report of only the same
    work or a subset does not. Worker completion, result delivery, foreground input, and
    reconnect do not by themselves reset it. When the main turn is stopped waiting on running
    work and elapsed time exceeds the cap, park with the cap reason and end the session. At
    the exact cap boundary, do not park. A foreground main turn is not capped. An operator
    resume after a cap park starts a fresh wait clock, as today.
11. **Failures are results for the main agent.** Deliver failed command exits and child-agent
    errors with their available outcome, exit status, and output/message. Failure alone does
    not park the task. The main agent can investigate and fix it under the existing retry/loop
    limits. A request for operator help and an existing limit still apply normally. A worker
    missing from inventory without an established outcome is not reported as a success.
12. **Keep stage and crash rules.** Stage signals such as `blocked`, `awaiting-answers`,
    `awaiting-ci`, `awaiting-review`, and `done` retain their existing transitions. A background
    result must not revive a parked, ended, canceled, or replaced launch. A dead Codex service
    is a session crash, with existing Failed and operator Resume behavior, rather than a live
    session whose control client is reconnecting. After results have been delivered, a new
    current main Stop with no running background work follows the normal input-park rule.
13. **Require compatible authoritative interfaces.** Use a tested Codex CLI version whose
    main-turn lifecycle, owned-work inventory, completion, delivery, and history interfaces
    pass compatibility checks. The installed 0.156.1 is the investigation baseline, not proof
    for another version. A failed required hook or unsupported/unreadable payload is not a
    turn end, empty inventory, or delivery receipt. Report the problem to the operator instead
    of falling back to unqualified legacy `notify` or transcript-format guesses.
14. **Make result-delivery problems visible.** Record task-scoped control/reconnect and delivery
    outcomes in the existing event/history surfaces. An uncertain-delivery alert identifies
    the task and pending result, says why automatic resend is held, and remains visible while
    unresolved. Repeated checks of the same unresolved delivery do not repeat the notification;
    confirmed receipt resolves that pending condition. Keep the existing background-cap note.
15. **Verify the behavior through the task flow.** Reproduce the stale and foreign marker bugs
    before fixing them. Acceptance coverage must exercise concurrent task isolation, active
    operator turns, background outcomes, disconnects, uncertain delivery, cap boundaries, and
    attached terminal behavior through the session/dispatcher boundary. Validate the actual
    herdr/Podman launch path before considering the integration complete. Run the repository's
    `make gate` for implementation changes.

## Scenarios

### Scenario: an old marker cannot park a new working main turn

- **Given** portfolio_eval #370 in `review`, a live bound main conversation, a Stop for main
  turn A, and its waiting marker
- **When** main prompt B starts and the next dispatcher pass runs while B is working
- **Then** A's marker is cleared, #370 remains In progress, and the session stays alive

### Scenario: a delayed old Stop arrives after a new main prompt

- **Given** #370 with main turn B working and the old waiting marker cleared
- **When** a delayed Stop for turn A arrives and the dispatcher runs
- **Then** it creates no current waiting marker and does not park or interrupt B

### Scenario: a title-thread notification arrives while the main turn works

- **Given** portfolio_eval #384 in `review`, a bound main conversation, and its active turn
- **When** Codex's internal title thread fires legacy `notify` with a different thread ID
- **Then** the main binding and markers stay unchanged, and #384 remains In progress

### Scenario: a child agent ends before its working main turn

- **Given** #370 with a working main turn and three task-owned child agents
- **When** one child's Stop or completion arrives
- **Then** that event is a worker result, not the main turn's Stop, and cannot park #370

### Scenario: unidentified input cannot establish a main turn end

- **Given** #384 with a live main session and a `working` stage signal
- **When** a notification has no usable session/turn identity, or identifies another task's
  conversation
- **Then** it establishes no current main Stop and cannot create a stopped-mid-stage park

### Scenario: a stopped main turn with no work parks normally

- **Given** #384's current bound main turn has stopped, its signal is `working`, no owned
  background work is running, and no completion is pending delivery
- **When** the dispatcher evaluates the task
- **Then** it parks with "(session stopped mid-stage waiting for input)" and ends the session

### Scenario: concurrent tasks do not share background results or prompts

- **Given** #281 and #384 running Codex with different bound conversations and workers
- **When** #281's command finishes while an operator prompt starts on #384
- **Then** the result reaches only #281 and the prompt reaches only #384; neither main binding
  changes to the other's conversation

### Scenario: the next ticket rejects an event from the previous ticket

- **Given** #370 has advanced from implement ticket 3 to ticket 4 with a new main conversation
- **When** a ticket-3 Stop or completion arrives late
- **Then** it does not change ticket 4's markers, wait clock, worker set, or main input

### Scenario: a resume uses the task's own recorded conversation

- **Given** #384 parked in `awaiting-spec-review`, with its valid `spec` conversation record,
  and #281's conversation is more recently used
- **When** #384 receives the operator reply "Approved, go on"
- **Then** it continues its recorded `spec` conversation and receives that reply there, with
  no use of the newest/last conversation selector

### Scenario: a resume with no valid conversation record starts the stage afresh

- **Given** #384 parked in `review` without a valid conversation record and a queued operator
  message "Check the API routes again"
- **When** the operator resumes it
- **Then** #154's fresh-review launch path delivers the queued message and logs why the
  conversation was not continued; no other task's conversation is selected

### Scenario: a background command outlives the main turn and stall timer

- **Given** #370's current main turn stopped with `working` and an owned test command still
  running, with `stall_after_seconds: 600`
- **When** 11 minutes pass without a new main turn and the dispatcher evaluates the task
- **Then** #370 remains In progress with its live session and held capacity slot

### Scenario: an owned agent keeps the main session available

- **Given** #370's current main turn stopped with `working`, no running command terminals,
  and a child review agent still running
- **When** the dispatcher evaluates the task
- **Then** the empty terminal list does not imply no background work; #370 stays In progress

### Scenario: a nested owned agent counts while unrelated threads do not

- **Given** #370 has a child agent that spawned a running grandchild, plus a title thread and
  another task's independent agent
- **When** #370's main turn stops and inventory is reconciled
- **Then** its running grandchild counts as background work; the unrelated threads do not

### Scenario: unavailable inventory does not mean the work finished

- **Given** a known running worker and a live session
- **When** its current inventory or outcome cannot be read
- **Then** no success is invented and no input park is justified solely by that missing view

### Scenario: the attached task terminal displays continuation

- **Given** #370's operator is attached to its existing Codex task terminal
- **When** an owned command exits and the idle bound conversation continues with its result
- **Then** the same terminal remains attached and displays the continuation and outcome

### Scenario: one worker finishes while another is still running

- **Given** #370's main conversation is idle and it has two running background workers
- **When** worker A finishes while worker B remains running
- **Then** A's result starts continuation of the same main conversation promptly, without
  waiting for B or the next 10-minute pass, and B retains the existing wait clock

### Scenario: several already-available results share one delivery

- **Given** three distinct completions are ready before the controller prepares the next
  main-agent delivery
- **When** it delivers them
- **Then** one message carries all three results and each completion has one delivery record

### Scenario: a completion reaches an already working main turn

- **Given** #384's main turn B is working after an operator prompt
- **When** its background command completes
- **Then** the result is accepted with B's exact turn ID and is processed within B, with no
  interruption and no second main turn

### Scenario: the active turn changes before delivery is accepted

- **Given** the controller prepares a result for main turn B
- **When** B ends and operator turn C starts before the result's acceptance
- **Then** the request tied to B is rejected, the controller reads current state, and the
  result is delivered into C without starting another main turn

### Scenario: an operator turn starts while an idle continuation is prepared

- **Given** the main conversation was idle when a completion became ready
- **When** operator turn C starts before the completion can be delivered
- **Then** the result reaches C in the same conversation, without interrupting it or creating
  a competing continuation turn

### Scenario: a lost acknowledgment is confirmed from history

- **Given** the controller sent a result with delivery ID D, Codex accepted it, and the
  connection dropped before the controller received acknowledgment
- **When** it reconnects and finds the accepted message with D in the bound history
- **Then** it marks that delivery successful and sends no duplicate result or continuation

### Scenario: receipt remains uncertain after reconnect

- **Given** delivery D was sent and its acknowledgment was lost
- **When** the controller cannot establish whether D was accepted
- **Then** D stays pending, the operator gets an alert explaining the uncertainty, the task and
  session stay running, and the controller checks history without blindly resending D

### Scenario: a duplicate completion event and history record create one delivery

- **Given** worker A's completion has already been delivered
- **When** a duplicate completion event arrives and reconnect also recovers A from history
- **Then** no second result delivery or continuation is created

### Scenario: restarting the controller preserves uncertain delivery

- **Given** delivery D is durably pending with unknown acceptance and a wait clock already set
- **When** the controller or dispatcher restarts while the Codex service remains alive
- **Then** it recovers D and that same clock, checks the bound history, and does not reset the
  uncertainty to an unsent message

### Scenario: a command finishes while the control client is offline

- **Given** a recorded running command, a stopped main turn, and a live Codex service/terminal
- **When** the control connection drops, the command finishes with exit code 0 and output
  `RECOVERY_OUTPUT`, and the client reconnects
- **Then** it recovers that command's completion and output from the same bound conversation,
  delivers it once under the receipt rules, and preserves the original wait clock

### Scenario: reconnect while a known worker is still running

- **Given** a stopped main turn and a known running background worker
- **When** the control client disconnects and later reconnects before that worker finishes
- **Then** the worker remains associated with the same task launch and existing wait clock,
  and no new conversation is started

### Scenario: the exact cap boundary keeps the background wait alive

- **Given** `background_wait_seconds: 10800`, a stopped main turn, and work first reported
  exactly 10800 seconds ago with no new work since
- **When** the dispatcher evaluates the task
- **Then** the task is not parked and the session remains alive

### Scenario: an idle background wait past the cap parks

- **Given** the same stopped main turn and still-running work with elapsed time 10801 seconds
- **When** the dispatcher evaluates the task with current authoritative state
- **Then** it parks with the existing 180-minute cap reason and ends the session and its work

### Scenario: repeated reports and partial completion preserve the clock

- **Given** workers A and B were first reported 2 hours 50 minutes ago
- **When** A finishes and its result is delivered, a later stopped turn reports only B, and the
  task is evaluated 20 minutes later
- **Then** B's wait remains measured from the original report and the idle wait is capped

### Scenario: genuinely new work resets the wait clock

- **Given** worker A was first reported 2 hours 50 minutes ago
- **When** the main agent starts new worker C, its next Stop reports C, and a pass runs 20
  minutes later while the main agent is stopped
- **Then** the wait is measured from C's new-work report and the task is not cap-parked

### Scenario: a foreground turn is not parked by the background cap

- **Given** worker A's wait clock is 3 hours 30 minutes old and the bound main turn is working
- **When** the dispatcher evaluates the task
- **Then** the background cap does not park or interrupt the main turn

### Scenario: an operator resume after a cap park starts a fresh clock

- **Given** #370 was parked at the background cap and its session was ended
- **When** an operator resumes it and that session reports new running background work
- **Then** its new background wait starts a fresh cap clock

### Scenario: a failed test is delivered for repair

- **Given** #370's idle main conversation is waiting on a background test command
- **When** the command exits with code 7 and output `FAILURE_OUTPUT`
- **Then** those failure details reach the main agent promptly, the worker is no longer
  running, and command failure alone does not park #370

### Scenario: a child-agent error is delivered for investigation

- **Given** #370's main agent has a task-owned child review agent
- **When** that agent ends with an error message
- **Then** the main agent receives the error outcome and message under the normal delivery
  rules; the controller does not relabel the error as success or park solely for that error

### Scenario: an existing retry limit still parks

- **Given** a background failure has reached the main agent and the task's existing gate-fix
  loop is at its configured cap
- **When** the stage reports that capped loop
- **Then** the existing limit park and operator notification apply

### Scenario: a stage request for help wins over background work

- **Given** a live session with a background worker still running
- **When** its stage signal becomes `blocked` with note "gate cannot run", or `awaiting-answers`
  with its existing operator artifact
- **Then** the corresponding existing operator park applies with its note/artifact

### Scenario: a completed or ended launch cannot be revived by a result

- **Given** a launch ended through `done`, `awaiting-ci`, `awaiting-review`, an operator park,
  cancellation, or replacement
- **When** its completion arrives late
- **Then** the event does not wake that ended launch or change the existing stage transition

### Scenario: the main turn stops again after consuming all results

- **Given** the main agent consumed its delivered completions and no worker remains running
- **When** its new current main turn stops with stage signal `working`
- **Then** the ordinary stopped-mid-stage input park applies

### Scenario: the Codex service dies

- **Given** a task is In progress with its Codex service running
- **When** that service dies while a stage is in flight
- **Then** the existing session-crash path marks the task Failed, preserves its resumable stage,
  and exposes Resume; control-client reconnect does not treat the dead service as live

### Scenario: an incompatible interface cannot supply turn-end evidence

- **Given** Codex's required lifecycle/inventory/history capability check fails, a required hook
  fails, or its payload cannot be read
- **When** the controller evaluates that information
- **Then** it reports the compatibility problem and derives no main Stop, empty worker set, or
  delivery receipt from it; unqualified legacy `notify` is not used as a fallback

### Scenario: an unresolved delivery alerts once and clears on receipt

- **Given** delivery D has uncertain receipt and its task-scoped operator alert is visible
- **When** three later checks remain uncertain and a fourth finds D accepted in history
- **Then** the first three checks do not repeat the notification, and the fourth records
  confirmed delivery and resolves the pending alert

### Scenario: the full task launch path preserves the terminal and task identity

- **Given** two Codex tasks launched through the real herdr/Podman path with the tested CLI
- **When** one operator submits a prompt while its worker completes, and the other task's
  control client reconnects after missing a completion
- **Then** both existing terminals stay attached, each result reaches its task's bound main
  conversation once under the receipt rules, and neither task is falsely parked

## Out of scope

- Auditing or repairing conversations polluted by the historical #154 resume bug.
- A box-wide Codex cap and the concurrent authentication refresh check in #155.
- Arbitrary detached process discovery outside owned Codex commands and agent descendants.
- Changing Claude's background-result continuation mechanism, the dispatcher pass interval,
  admission/capacity policy, existing retry/loop limits, or stage gates.
- Automatic recovery of a dead session service or a server-side exactly-once guarantee for
  repeated client message IDs.
- Deployment, live task resumes, or edits to the separate #154 branch.
