# Plan gate: an approved plan must not go back to review — Design

## Decisions

- **One rule at the fresh-launch funnel, keyed on `gated`: a plan launch of a gated task saves
  stage `awaiting-plan-review`.** Every fresh plan launch (a wake or a retry with no matching
  session record, the respawn of a dead session, the crash Resume through `_respawn`) goes
  through `_launch_stage`, so one rule there covers all callers. The rule reads `gated`, not the
  current stage, because a crash Resume starts from stage `failed` (then `plan`, set by
  `_apply_resume_intent`). An ungated plan launch still sets `plan`. — "Stage `plan` and `gated`"
  becomes a state that new code never writes; the stage no longer tells "a plan session runs"
  from "the plan waits at the gate", and the stage signal and `operator_request` tell that, as
  they do for a resumed conversation today. A crash-resumed session can now also report an
  unapproved `done` (red-team #1, accepted).
- **A crash Resume at the gate uses no extra unattended round.** The operator's Resume goes
  through `_wake`, which sets `unattended_rounds` to the operator-acted value, so the next ready
  report at the gate brings it to 0, the same as an operator wake. No new accounting is needed for
  this path.
- **A respawn at the gate and its first ready report count one unattended round together, as
  today.** The respawn already counts a round (`loops.gate_fields`). Its first ready report at the
  gate must not count a second one (red-team #3). — The round accounting must see that the
  ready report follows a respawn; today that came free from the detour through stage `plan`.
- **The approval stays session-reported.** The dispatcher accepts a `done` at the gate as the
  approval, as today, and does not read `review-answers.json` (proposal non-goal). — A respawned
  or crash-resumed session at the gate can now report `done` without an approval (red-team #1);
  the operator accepted this risk.
- **A `done` from a gated task in stage `plan` (state saved before this change) parks for the operator with reason
  `WHY_BAD_SIGNAL` and a note of its own.** The note says that the gate state was lost, names the
  reported track, and says that the task can be moved to implement by hand. No retry is spent and
  no `plan_retry` is sent. An ungated task keeps the `PLAN_NO_APPROVAL` bounce. — No new park
  reason in the console vocabulary. The note, not the reason, carries the meaning.
- **The dispatcher writes `.agent/task.json` again before every launch and every resume, from the
  `TaskState`**, at the two funnels every session start goes through: `_launch_stage` (fresh
  launches) and `_continue_or_restart` (resumes and fresh restarts). The write uses
  `write_worktree_file`, as `create_workspace` does. — The values come from the dispatcher's
  state, never from the file, so a session change is undone at the next launch. A failed write is
  logged and the launch goes on, as for `_heal_exclude`: a worktree with no identity file
  behaves as today.
- **waitd and the Stop hook do not change.** A healed file is all they need to record the
  conversation. — The legacy target-less ping path stays for worktrees that are not launched
  again.

## Data model

No database. The state is JSON files; the invariants are kept in code and asserted by tests.

| Table | Field | Constraint | Why |
|---|---|---|---|
| `task-<target>-<issue>.json` (`TaskState`) | `stage` | A plan launch of a task with `gated: true` saves stage `awaiting-plan-review`; no launch writes stage `plan` with `gated: true`. | Requirement 1: the `done` that follows is read as the approval. |
| `TaskState` | `gated` | Never cleared (unchanged). A `done` with `gated: true` in stage `plan` parks; it never bounces. | Requirement 3. |
| `TaskState` | `unattended_rounds` | A respawn at the gate plus its first ready report add 1, not 2. | Red-team #3: the round limit (2) must keep its meaning. |
| `<worktree>/.agent/task.json` | `issue`, `target`, `branch` | All three present and equal to the `TaskState` values after each launch and resume. Shape `{"issue": int, "target": str, "branch": str}`. | Requirements 4 and 5: the Stop hook pings with the target, and the push guardrail reads the branch. |
| `session-<target>-<issue>` | `session_id`, `stage` | Unchanged; written by waitd from a ping with a target. | Requirement 5. |

## Identities

None.

## Seams

- `dispatcher.main.run_pass` (existing), over the fakes of `tests/test_main.py` with a real
  worktree, as in `tests/test_plan_review_gate_acceptance.py`. Drives the wake, retry, respawn,
  `done` and park scenarios, and the `.agent/task.json` heal before a launch or resume.
- `dispatcher.waitd.handle_ping` (existing). Drives the turn end of a healed worktree, so that
  the record it writes is what the next `run_pass` wake resumes.
