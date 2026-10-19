# Plan gate: an approved plan must not go back to review

## Why

The operator approved the plan of portfolio_eval #283 five times between 2026-10-14 and
2026-10-15, and every approval brought the same plan back for review. Two defects cause the
loop:

1. **A fresh plan session from the gate drops the gate stage.** When a task at
   `awaiting-plan-review` wakes and the dispatcher has no recorded conversation for it, the
   dispatcher starts a fresh plan session, and that launch writes stage `plan` on the task. The
   session then reads the approval and reports `done`. A `done` in stage `plan` means "nobody
   approved this plan", so the dispatcher tells the session to ask for review again. With a
   recorded conversation the same approval works, because a resume keeps the gate stage. The
   respawn of a dead session at the gate drops the gate stage in the same way.
2. **A worktree made before the multi-project rename never records its conversation.** The
   worktree of #283 was made on 2026-08-26. Its `.agent/task.json` is `{"issue": 283}`, with no
   `target` and no `branch`. The Stop hook sends an empty target, waitd takes the legacy path,
   and no conversation is recorded. So every wake of #283 started a new conversation (13 times),
   and every one of them hit defect 1. The missing `branch` also makes the push guardrail refuse
   lease pushes for that task.

Each round costs the operator a review and a reply for a plan that did not change, and the task
never reaches implement.

## For whom

The operator of the box, who reviews plans at the gate and must be asked once per plan revision.
The unattended plan sessions, which lose their conversation and their approval.

## Goal

An operator's approval at the plan gate takes the task to implement, whether the plan session
continues its recorded conversation or starts a new one. A task whose worktree predates the
current `.agent/task.json` shape records its conversation again from its next launch or resume.

## Non-goals

- No automatic repair of tasks that already lost the gate stage (portfolio_eval #283 today). The
  operator recovers #283 by hand after the fix ships.
- No change to who reports the approval: the plan session still reports `done` at the gate. The
  dispatcher does not read `review-answers.json` to decide.
- No change to the review page, the answers protocol, the grace clock, or the unattended-round
  limit.
- No removal of waitd's legacy (target-less) ping path, and no cleanup of the stale legacy
  `waiting-<issue>` markers in the box's state dir.
- No change for Codex conversation recording beyond what `task.json` provides.
