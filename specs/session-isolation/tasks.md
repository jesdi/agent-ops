# Session isolation: a resume continues only the task's own conversation — Tasks

Each task links to the goal in proposal.md: a resume continues exactly the conversation that the
task's current stage recorded, on both runtimes, or starts the stage again with a fresh session;
a Codex subagent's turn end neither parks the task nor changes which conversation it resumes.
Work the frontier (tasks whose blockers are done), commit per task with its ID, and finish every
task with `make test` green. Tests come first in every task.

- [ ] **T1** — waitd records a session. A ping with a `session_id` and no `runtime`, for a task
  that has a state file, writes the session record with that ID and the task's continued stage
  (`spec` for `awaiting-spec-review`, the task's stage otherwise); a later ping replaces it. A
  ping with no `session_id`, an empty `target`, or no state file for the task writes no record.
  The waiting and background markers behave exactly as today in every case. The record is
  written atomically; an unreadable file or one missing a field reads as absent;
  `clear_session` removes it, and a missing record is not an error. Covers "a Claude turn end
  records the session" (waitd half), "a later turn end replaces the record" and "an unreadable
  record is not used" (read half). _Goal: the box knows which conversation a task's stage is in._
  Seam: `dispatcher.waitd.handle_ping(body, state_dir)`, read back with
  `dispatcher.state.read_session` / `clear_session` / `has_waiting` / `read_background`.
  Blocked by: none.
- [ ] **T2** — waitd classifies a Codex turn end. A ping with `runtime: "codex"` is judged from
  the thread's rollout file under `<state_dir>/codex-home/sessions/`: a subagent thread drops
  the ping whole (no marker written or removed, record unchanged); a root thread whose `cwd` is
  the task's worktree writes the record and the waiting marker; anything else — no `session_id`,
  no rollout file, an unreadable or shapeless first line, a root thread with another `cwd` —
  writes the waiting marker and leaves the record unchanged. Covers "a Codex root turn end
  records the thread", "a Codex subagent's turn end is ignored" (waitd half), "a Codex turn end
  the hook cannot identify", "a Codex thread of another worktree is not recorded" and "a Codex
  turn end with no thread ID" (waitd half). _Goal: a subagent neither parks the task nor
  replaces its conversation, and another task's thread is never recorded._
  Seam: `dispatcher.waitd.handle_ping(body, state_dir)` with rollout files placed under the
  state dir, read back with `read_session` / `has_waiting`. Blocked by: T1.
- [ ] **T3** — The stop hook forwards the session ID. Run as Claude's hook (no argument), it
  sends the hook input's `session_id`, together with `background_tasks` when today's rules
  forward them. Run as Codex's `notify` (JSON argument, stdin not read), it sends the argument's
  `thread-id` as `session_id` with `runtime: "codex"`. With unusable input (empty, `garbage`,
  `{}`, a non-JSON argument, a non-string ID) it sends today's ping, with `runtime: "codex"` when
  it has an argument. It exits 0 in every case, with waitd up or down. Covers "a Codex root turn
  end records the thread", "a Claude turn end records the session", "a Codex subagent's turn end
  is ignored", "three subagents finish, then the root turn ends", "a Codex turn end with no
  thread ID" and "a Claude background wait still works", end to end. _Goal: every root turn end
  tells the box its conversation._
  Seam: `hooks/stop-hook.sh` as a subprocess against a served waitd, read back with
  `read_session` / `has_waiting` / `read_background`. Blocked by: T1, T2.
- [ ] **T4** — A resume names the recorded session. `Runtime.resume` and `resume_cmd` take the
  session ID: Codex builds `resume <id> <message>`, Claude `--resume <id> <message>`; no command
  contains `--last` or `--continue`. A parked task with a valid record is resumed by that ID in
  its own worktree, for an operator wake and for an attach (notification note empty, as today).
  Every fresh launch — a stage, and each implement ticket — removes the record before the
  session starts, and the task's flush removes it too. The crash repro line prints the recorded
  ID. Update the resume lines of `docs/specs/2026-09-24-codex-runtime-design.md` and the
  `sessions.py` docstring, and add **Session record** to `CONTEXT.md`. Covers "a Codex resume
  names the recorded thread", "a Claude resume names the recorded session", "two parked Codex
  tasks, resumed in the opposite order of their last activity", "a resume while another task's
  Codex session is live", "a fresh launch discards the record", "the next implement ticket
  discards the record" and "an attach with a record". _Goal: a resume continues exactly the
  recorded conversation and never one chosen by recency._
  Seam: `dispatcher.main.run_pass` with recording `Deps`; the command shape through
  `dispatcher.sessions.Sessions.resume` / `spawn_stage` and `dispatcher.runtimes.Runtime.resume`.
  Blocked by: T1.
- [ ] **T5** — Without a valid record the stage starts again. For an operator wake, an attach, a
  plan retry and a spec retry of a task whose record is absent, unreadable or of another stage:
  no resume command is launched; the continued stage is launched fresh in the same worktree
  (the current ticket for implement, `spec` for `awaiting-spec-review`); the message the resume
  would have delivered and the queued operator messages are in the prompt and are marked
  delivered; retry counters still count; the `resumed` event has the detail `new conversation: no
  session recorded` or `new conversation: session recorded for <stage>`; the attach notification's
  note is `new conversation`. Covers "a task parked before this change starts its stage again",
  "a record from another stage is not used", "an unreadable record is not used", "an in-session
  retry without a record", "an attach without a record" and "a resume in awaiting-spec-review
  without a record". _Goal: a missing record costs a conversation, never a wrong one._
  Seam: `dispatcher.main.run_pass` with recording `Deps`, observed through the sessions calls,
  the event log, the notifier and the delivered messages. Blocked by: T4.
