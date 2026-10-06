# Session isolation: a resume continues only the task's own conversation — Design

## Decisions

- **The hook reports, waitd records, the dispatcher decides.** The stop hook adds the session ID
  to its existing ping; waitd writes the session record; the dispatcher reads it before a resume
  — the same split as the background wait, so the hook stays a thin script and every rule lives
  in testable Python, but a ping sent while waitd is down records nothing and that turn's
  conversation can only be resumed if an earlier turn end recorded it.
- **The record lives in the state dir, not in the worktree.** A session container mounts only the
  wait socket's directory, so a session cannot write or edit its own record — the record is not
  beside `stage.json`, where an operator would look first.
- **One ping endpoint, two extra fields.** `session_id` (string) and `runtime` (`"codex"` when the
  hook ran as Codex's `notify`, absent for Claude). An old waitd ignores both and parks as today;
  an old hook sends neither and the task gets no record, so hook and waitd can deploy in either
  order.
- **Claude vs Codex is told by arguments, as today.** No argument: Claude, `session_id` read from
  the hook input on stdin. An argument: Codex, `thread-id` read from the JSON argument (verified
  2026-10-06 on codex-cli 0.156.1 and 0.160.0) — a Codex release that renames the field degrades
  every Codex resume to a fresh launch, never to a wrong conversation.
- **waitd classifies a Codex turn end from the thread's own rollout file.** The file is found by
  the thread ID under `<state_dir>/codex-home/sessions/`. Its first line's `source` tells a
  subagent (an object with a `subagent` key) from a root session (a string), and its `cwd` names
  the directory the thread was started in — this reads a file format Codex owns; when the file,
  the line or a field is missing or changes shape, the turn end is "unidentified" (requirement 7).
- **A Codex root session is recorded only when its thread's `cwd` is the task's worktree.** This is
  the identity check: a thread that belongs to another task is never recorded, whatever ID a ping
  carries — a session that changes its launch directory away from the worktree is never recorded
  either, and resumes as a fresh launch.
- **A subagent ping is dropped whole.** No waiting marker, no background marker, no record, and
  the existing markers stay as they were.
- **The record's stage is the continued stage of the task at the ping.** waitd reads the task's
  state file; `awaiting-spec-review` maps to `spec`, every other stage to itself — a ping for a
  task with no state file, or with an empty `target`, records nothing and parks as today.
- **Only a fresh launch and the task's flush remove the record.** `Sessions.spawn_stage` removes
  it before the session starts; a park, a session end and a resume keep it. Every caller ends the
  old session before a fresh launch, so a ping still in flight inside waitd at that moment is the
  only way an old ID can be written after the removal (ceiling: one request; the stage in the
  record catches it unless the next launch is the same stage, as between two implement tickets).
- **The latest root turn end wins.** Each recorded turn end replaces the record, so a runtime
  that gives a resumed conversation a new ID is followed.
- **A resume takes the session ID as an argument; nothing asks for "the newest".** The runtime
  record builds `resume <id> <message>` for Codex and `--resume <id> <message>` for Claude.
  `--last` and `--continue` are removed from the code, and the crash repro line prints the
  recorded ID.
- **One function decides continue-or-restart for every resume site.** The operator wake, the
  attach, the plan retry and the spec retry all go through it: a valid record resumes by ID;
  otherwise the continued stage is launched fresh through the ordinary stage launch, with the
  resume's message appended to the stage prompt — the fresh session re-reads the stage prompt
  and has no memory, so a plan or spec retry without a record redoes that stage, and a wake in
  `awaiting-spec-review` without a record returns the task to `spec`.
- **A restart keeps the task's counters and ticket.** The implement ticket is the task's current
  one, and a retry still counts as a retry — only the conversation is new.
- **The event log carries the reason.** The `resumed` event gets the detail `new conversation: no
  session recorded` or `new conversation: session recorded for <stage>`; a resume by ID keeps
  today's empty detail.
- **The attach notification's note says `new conversation`** when the attach starts a fresh
  launch; otherwise the note is empty as today.
- **No migration.** A task parked before the deploy has no record and takes the restart path once.
- **Checked on the box after deploy, not by the suite.** Two Codex tasks parked at the same time
  and resumed in the opposite order of their last activity; each rollout file must show one
  worktree only. A Codex stage that uses subagents must reach its own turn end unparked.

## Data model

No database. Files in the dispatcher's state dir and fields of the ping.

| Table | Field | Constraint | Why |
|---|---|---|---|
| `session-<target>-<issue>` (record file, written only by waitd) | `session_id` | non-empty string | the conversation a resume names |
| | `stage` | the task's continued stage when the turn end was recorded | a record of another stage is not valid |
| | whole file | written atomically (temporary file, then rename); an unreadable file, or one missing a field, reads as absent | the dispatcher never resumes from half a record |
| | lifetime | removed only by a fresh launch and by the task's flush | a record never outlives the stage or ticket that wrote it, and survives a park |
| ping body (hook to waitd) | `session_id` | optional string | the turn end's conversation |
| | `runtime` | optional; `"codex"` or absent | selects the Codex classification |
| Codex rollout file (read only, owned by Codex) | first line `payload.source`, `payload.cwd` | read, never written | root or subagent, and whose worktree |

A turn end, as waitd sees one ping for a known task:

| Ping | Record | Markers |
|---|---|---|
| Claude, with `session_id` | written | as today (waiting or background) |
| Claude, no `session_id` | unchanged | as today |
| Codex, thread is a subagent | unchanged | unchanged (ping dropped) |
| Codex, thread is root and its `cwd` is the task's worktree | written | waiting, as today |
| Codex, anything else (no ID, no rollout file, unreadable line, other `cwd`) | unchanged | waiting, as today |

## Identities

None.

## Seams

- `hooks/stop-hook.sh` run as a subprocess against a served waitd, with the hook input on stdin
  (Claude) or as an argument (Codex) — existing; observed through the state functions below.
- `dispatcher.waitd.handle_ping(body: bytes, state_dir) -> None` — existing; new body fields
  `session_id` and `runtime`. Codex cases read a rollout file placed under
  `<state_dir>/codex-home/sessions/`.
- `dispatcher.state.read_session(state_dir, target, issue) -> SessionRecord | None` and
  `dispatcher.state.clear_session(state_dir, target, issue) -> None` — new. `SessionRecord` has
  `session_id: str` and `stage: str`.
- `dispatcher.runtimes.Runtime.resume(session_id: str, message: str) -> str` and
  `Runtime.resume_cmd(session_id: str, message: str = "") -> str` — existing, new first
  argument.
- `dispatcher.sessions.Sessions.resume(..., session_id: str, ...)` and
  `Sessions.spawn_stage(...)` — existing; observed through the command typed into the herdr tab
  and through `read_session`.
- `dispatcher.main.run_pass` with recording `Deps` — existing; the seam for every resume site
  (wake, attach, plan retry, spec retry), observed through the sessions calls, the event log, the
  notifier and the delivered messages.
