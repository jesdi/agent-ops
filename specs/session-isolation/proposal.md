# Session isolation: a resume continues only the task's own conversation

## Why

A resumed Codex session can continue the conversation of a different task. On 2026-10-06
portfolio_eval #384 was resumed twice, and each time Codex continued another task's thread: at
06:48 the #281 review thread, at 07:56 the #370 review thread. The session then worked in the
#384 worktree with the other task's assignment, tool history and results in its context, and the
operator had to tell it which directory to use. Task #384's own thread existed and was never
selected (jesdi/agent-ops#154).

The cause is that a resume asks the CLI for "the newest session" (`codex resume --last`,
`claude --continue`) and trusts the CLI to mean "the newest session of this worktree". On the box
Codex selected the newest session of all tasks. Claude has shown no cross-task failure, but its
resume is implicit in the same way, and the newest conversation in a worktree can belong to an
earlier stage.

The same investigation found a second defect in the same hook. A Codex stage that uses subagents
is parked while it works: on 2026-10-06 the #370 review session was parked at 08:23 with "(session
stopped mid-stage waiting for input)" after its three review subagents completed at 08:16, and its
own turn never completed. The turn end of a subagent is treated as the turn end of the session.

## For whom

The operator of the box, whose replies must reach the task they were written for, and the
unattended stage sessions, which must never work with another task's context or be stopped while
their subagents report.

## Goal

A resume continues exactly the conversation that the task's current stage recorded, on both
runtimes, or starts the stage again with a fresh session when there is no such record. It never
continues a conversation chosen by recency. A Codex subagent's turn end neither parks the task
nor changes which conversation the task resumes.

## Non-goals

- No audit of what the #384 session did while it carried the #281 and #370 context. Decided
  2026-10-06: the code is fixed, the past actions are not examined.
- No repair of the polluted #281 and #370 threads. After this change no task resumes them.
- No box-wide cap on live Codex sessions. Two Codex tasks may run at the same time.
- No check of concurrent refresh of the shared `auth.json` (jesdi/agent-ops#155).
- No explanation of why Codex's working-directory filter does not apply on the box. The fix does
  not depend on it.
- No automated test that runs a real, logged-in CLI, and no manual reproduction with one. Proof is
  the acceptance tests at the dispatcher seam plus a check on the box after deploy.
- No per-task runtime home (`codex-home`, `claude-home` stay shared).
- No change to a fresh stage launch: it already starts a new conversation.
- No change to how a Claude background wait works.
- No continuation of a conversation across stages or across runtimes.
- No prompt changes.
