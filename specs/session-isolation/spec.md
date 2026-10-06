# Session isolation: a resume continues only the task's own conversation — Spec

Terms:

- A **conversation** is what a runtime keeps for one session and can continue: a Codex thread, a
  Claude Code session. Its **session ID** is the runtime's own identifier for it.
- A **fresh launch** starts a stage, or one implement ticket, with a stage prompt and a new
  conversation. A **resume** relaunches the runtime to continue a conversation, with a message.
- A **root session** is the session the dispatcher launched. A **subagent** is a session that a
  root session started itself.
- A **turn end** is the runtime running the stop hook: Claude Code's Stop hook, Codex's `notify`
  program.
- The **session record** is what the box holds for a task about the conversation of its current
  fresh launch: the session ID, and the stage that launch belongs to. A session cannot write it.
- The **continued stage** of a resume is the stage whose session it continues: `spec` for a task
  in `awaiting-spec-review`, the task's own stage otherwise.
- A session record is **valid** for a resume when it exists, is readable, names a session ID, and
  its stage is the resume's continued stage.

## Requirements

1. **A root session's turn end records its conversation.** Every turn end of a root session
   writes the session record with that session's ID, on both runtimes, replacing any earlier
   record. The hook always exits 0.
2. **A resume names the recorded conversation.** When a task with a valid session record is
   resumed, the resume command names exactly the recorded session ID. No resume command, on
   either runtime, asks for the newest or last conversation.
3. **A fresh launch discards the record.** Every fresh launch removes the task's session record
   before the session starts, so a record never outlives the stage or the ticket that wrote it.
4. **Without a valid record the stage starts again.** When a task without a valid session record
   is resumed, no conversation is continued. The resume's continued stage starts again with a
   fresh launch in the same worktree. The message the resume would have delivered, queued operator
   messages included, is part of that launch's prompt, and those messages are marked delivered.
   The event log says that the conversation was not continued and why. This holds for every
   resume: an operator wake, an attach, and the dispatcher's own in-session retries.
5. **An attach is told when the conversation is new.** When requirement 4 applies to a task the
   operator is attaching to, the notification says that the session is a new conversation.
6. **A Codex subagent's turn end is ignored.** When the hook runs for a Codex subagent, it does
   not write the session record and does not report a turn end: the task is not parked and the
   root session stays alive.
7. **An unidentified Codex turn end parks but records nothing.** When a Codex turn end carries no
   session ID, or a session ID that cannot be shown to be a root session started in the task's
   own worktree, the turn end is reported as today and the session record stays as it was.
8. **Tasks never share a conversation.** With any number of tasks parked or running at the same
   time, on the same runtime and with resumes in any order, each resume continues only its own
   task's recorded conversation in its own worktree, and an operator reply is delivered only to
   the task it was sent to.
9. **Other turn-end behaviour is unchanged.** A Claude turn end with background work and a
   `working` stage signal is still a background wait, and every other root turn end still parks,
   exactly as today.
10. **In-flight tasks need no migration.** A task that was parked before this change has no
    session record and is handled by requirement 4 on its next resume. Every launch and resume
    installs the current hook, as today.

## Scenarios

### Scenario: a Codex root turn end records the thread

- **Given** portfolio_eval #384 in `spec` on `openai/gpt-6-astra`, with no session record, and a
  Codex root thread `01a0fc74-9f0c-75f3-8b77-a5b5e6dfb884` running in its worktree
- **When** Codex runs the hook for that thread's turn end
- **Then** the hook exits 0, the session record of #384 names `01a0fc74-9f0c-75f3-8b77-a5b5e6dfb884`
  and stage `spec`, and a waiting ping for #384 is sent

### Scenario: a Claude turn end records the session

- **Given** portfolio_eval #370 in `implement` on `anthropic/claude-opus-5-5`, with no session
  record
- **When** Claude Code runs the Stop hook with hook input that carries `session_id`
  `7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d`
- **Then** the hook exits 0 and the session record of #370 names
  `7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d` and stage `implement`

### Scenario: a later turn end replaces the record

- **Given** #370 with a session record that names `7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d`
- **When** the Stop hook runs with `session_id` `9d8c7b6a-5e4f-4a3b-8c2d-1e0f9a8b7c6d`
- **Then** the session record names `9d8c7b6a-5e4f-4a3b-8c2d-1e0f9a8b7c6d`

### Scenario: a Codex resume names the recorded thread

- **Given** #384 parked in `awaiting-spec-review` on `openai/gpt-6-astra`, its session record
  naming `01a0fc74-9f0c-75f3-8b77-a5b5e6dfb884` and stage `spec`
- **When** the operator replies "Approved, go on" and a dispatcher pass runs
- **Then** the command launched in #384's worktree resumes `01a0fc74-9f0c-75f3-8b77-a5b5e6dfb884`
  with the reply, and does not contain `--last`

### Scenario: a Claude resume names the recorded session

- **Given** #370 parked in `implement` on `anthropic/claude-opus-5-5`, its session record naming
  `7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d` and stage `implement`
- **When** the operator replies and a dispatcher pass runs
- **Then** the command launched in #370's worktree resumes `7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d`
  with the reply, and does not contain `--continue`

### Scenario: two parked Codex tasks, resumed in the opposite order of their last activity

- **Given** #281 and #384, both parked on `openai/gpt-6-astra`; #384's record names thread
  `01a0fc74-…` and #281's names `01a0fd57-…`; #281's thread is the more recently used one
- **When** the operator replies "Use the token" to #384 and "Rerun the gate" to #281, and a
  dispatcher pass runs
- **Then** #384's worktree gets a command that resumes `01a0fc74-…` with "Use the token", #281's
  worktree gets a command that resumes `01a0fd57-…` with "Rerun the gate", and neither command
  names the other task's thread, worktree or reply

### Scenario: a resume while another task's Codex session is live

- **Given** #370 with a live Codex review session that started 11 seconds ago, and #384 parked
  with a record that names `01a0fc74-…`
- **When** #384 is resumed
- **Then** the command resumes `01a0fc74-…` in #384's worktree, and #370's session and session
  record are untouched

### Scenario: a fresh launch discards the record

- **Given** #384 with a session record that names `01a0fc74-…` and stage `spec`
- **When** the dispatcher starts #384's `plan` stage
- **Then** #384 has no session record when the plan session starts

### Scenario: the next implement ticket discards the record

- **Given** #370 in `implement`, ticket 3 of 4 done, its session record naming the ticket-3
  session
- **When** the dispatcher starts ticket 4
- **Then** #370 has no session record when the ticket-4 session starts

### Scenario: a task parked before this change starts its stage again

- **Given** #281 parked in `review` on `openai/gpt-6-astra` with no session record, and a queued
  operator message "Check the API routes again"
- **When** the operator's wake is applied
- **Then** no resume command is launched; a fresh `review` session starts in #281's worktree, its
  prompt contains "Check the API routes again", the message is marked delivered, and the event
  log has an entry for #281 that says the conversation was not continued because no session was
  recorded

### Scenario: a record from another stage is not used

- **Given** #384 parked in `review`, with a session record that names `01a0fc74-…` and stage
  `spec`
- **When** #384 is resumed
- **Then** no command names `01a0fc74-…`; a fresh `review` session starts, and the event log says
  the conversation was not continued because the record belongs to another stage

### Scenario: an unreadable record is not used

- **Given** #384 parked in `review`, its session record file containing `{"session_id":`
- **When** #384 is resumed
- **Then** a fresh `review` session starts and no resume command is launched

### Scenario: an in-session retry without a record

- **Given** #384 in `plan` on `openai/gpt-6-astra`, its session exited with a plan that fails the
  format check, and no session record
- **When** the dispatcher retries the plan
- **Then** a fresh `plan` session starts whose prompt contains the retry instruction, and no
  resume command is launched

### Scenario: an attach without a record

- **Given** #281 parked in `review` with no session record
- **When** the operator attaches
- **Then** a fresh `review` session starts, and the attach notification says the session is a new
  conversation

### Scenario: an attach with a record

- **Given** #281 parked in `review`, its session record naming `01a0fd57-…` and stage `review`
- **When** the operator attaches
- **Then** the command resumes `01a0fd57-…`, and the attach notification is today's

### Scenario: a Codex subagent's turn end is ignored

- **Given** #370 in `review` on `openai/gpt-6-astra` with stage signal `{"stage": "review",
  "status": "working"}`, its session record naming root thread `01a11028-…`, and a subagent
  thread `01a11032-5278-…` started by that root thread
- **When** Codex runs the hook for the subagent thread's turn end, and a dispatcher pass runs
- **Then** the hook exits 0, no waiting ping is sent, the session record still names
  `01a11028-…`, the task is not parked, and the session is still alive

### Scenario: three subagents finish, then the root turn ends

- **Given** the same task, with three subagent threads
- **When** the hook runs once for each subagent's turn end and then for the root thread's turn end
- **Then** exactly one waiting ping is sent, after the root thread's turn end, and the session
  record names `01a11028-…`

### Scenario: a Codex turn end the hook cannot identify

- **Given** #370 in `review` on Codex, its session record naming `01a11028-…`
- **When** Codex runs the hook with a thread ID for which no thread information can be found
- **Then** the hook exits 0, a waiting ping is sent, and the session record still names
  `01a11028-…`

### Scenario: a Codex thread of another worktree is not recorded

- **Given** #384 in `spec` on Codex, with no session record, and a Codex root thread `01a0fd57-…`
  that was started in the worktree of #281
- **When** Codex runs #384's hook with thread ID `01a0fd57-…`
- **Then** the hook exits 0, a waiting ping for #384 is sent, and #384 still has no session record

### Scenario: a resume in awaiting-spec-review without a record

- **Given** #384 parked in `awaiting-spec-review` with no session record, and a queued operator
  message "Drop the second endpoint"
- **When** the operator's wake is applied
- **Then** a fresh `spec` session starts in #384's worktree, its prompt contains "Drop the second
  endpoint", and no resume command is launched

### Scenario: a Codex turn end with no thread ID

- **Given** #370 in `review` on Codex, with no session record
- **When** Codex runs the hook with an argument that is not JSON
- **Then** the hook exits 0, a waiting ping is sent, and #370 still has no session record

### Scenario: a Claude background wait still works

- **Given** #329 in `review` on Claude with stage signal `{"stage": "review", "status":
  "working"}`
- **When** the Stop hook runs with hook input that carries a `session_id` and `{"background_tasks":
  [{"type": "shell", "command": "make crap-gate"}]}`
- **Then** the session record names that `session_id`, the ping forwards the background task, and
  the task is not parked

## Out of scope

- An audit of what the #384 session did with the #281 and #370 context, and any repair of the
  polluted threads.
- A cap on live Codex sessions, and the concurrent `auth.json` refresh check (jesdi/agent-ops#155).
- Tests against a real, logged-in CLI.
- Per-task runtime homes.
- Continuing a conversation across stages or runtimes.
