# PR lifecycle: review feedback loop, Done column, flush

Date: 2026-07-30
Status: draft — pending human review

## Problem

`pr-open` is a dead end. When the implement stage opens a PR the dispatcher
sets the task to `pr-open`, pings the operator, and never touches it again:
no PR polling, no board-status update, no worktree cleanup, no state-file
removal. Cards accumulate forever in the console's "PR open" column, review
feedback on the PR goes unnoticed, and merged work is never marked done
anywhere.

## Goals

- Detect, per convergence pass, what happened to each open PR: merged,
  closed without merge, or new human feedback.
- New feedback reopens the task: a fresh `address-review` stage addresses
  the comments and returns the task to `pr-open`, looping until the
  reviewer is satisfied.
- Merge moves the task to a new terminal `done` stage: board status set to
  Done, worktree and branch removed, card shown in a new console **Done**
  column, state flushed after a retention window (default 7 days).
- When reaction is deferred by capacity/budget, the card visibly says so
  (`feedback_pending` badge).

## Non-goals

- No auto-merge. Merging is always a human act; a green, approved, unmerged
  PR sits in "PR review" — that is the steady state, not an error.
- No webhooks or new daemons. Detection rides the existing 10-minute
  dispatcher pass (ADR-0001: pull-based convergence).
- No handling of PRs the dispatcher didn't open.

## Lifecycle

```
… implement → pr-open ⇄ address-review     (feedback loop, repeats)
              pr-open → done               (PR merged; flushed after 7 days)
              pr-open → failed             (PR closed without merging)
```

New `Stage` members: `ADDRESS_REVIEW = "address-review"`,
`DONE = "done"`.

- `ADDRESS_REVIEW` joins `IN_FLIGHT_STAGES` — it holds a session, a slot,
  and capacity like any working stage.
- `PR_OPEN` and `DONE` remain slotless and hold no capacity.
- `NEXT_STAGE[ADDRESS_REVIEW] = PR_OPEN`: the stage's `done` signal returns
  the task to watching, via the existing `machine.next_actions` path (no
  artifact checker; the PR itself is the artifact).
- The `awaiting-ci` park works unchanged for `address-review` — the stage
  re-runs the same verification ladder as implement, including the E2E
  park/resume cycle.

## Task state additions

`TaskState` gains four fields (all with defaults, so existing state files
load unchanged):

| field | type | meaning |
|---|---|---|
| `pr_number` | `int = 0` | the task's PR; 0 = not yet known |
| `feedback_cursor` | `str = ""` | ISO timestamp; feedback newer than this is "new" |
| `feedback_pending` | `bool = False` | feedback seen, `address-review` spawn deferred |
| `done_at` | `str = ""` | merge-detection time; drives the flush |

**PR number capture.** On the implement `done` transition the dispatcher
parses the PR number from `signal.artifact` (the PR URL the implement
prompt writes into `stage.json`) and stores it; `feedback_cursor` stays
empty (= any human feedback counts). If parsing fails, the first poll
resolves it via
`gh pr list --head <branch> --json number`; a task whose PR cannot be
resolved after that logs a warning each pass and stays in `pr-open`.

## Detection: PR polling in the pass

A new pass step (shape of `_wake_ci`) runs for every `pr-open` task of each
target — unconditionally, since watching costs one API call and no
capacity:

```
gh pr view <pr_number> --repo <repo> --json \
    state,mergedAt,reviewDecision,reviews,comments
```

Classification, in priority order:

1. **Merged** (`mergedAt` set) → done path.
2. **Closed unmerged** (`state == CLOSED`) → stage `failed`, event
   `pr-closed`, Telegram notify. Worktree preserved for autopsy; board
   status untouched (the issue is still open — closing unmerged is a human
   judgment the dispatcher doesn't second-guess).
3. **New feedback** → reopen path. Feedback = any review (any state,
   including approvals with comments) or PR comment whose timestamp is
   newer than `feedback_cursor`, authored by neither the box's own GitHub
   login (fetched once per process via `gh api user`, cached) nor a bot
   (login ends in `[bot]`). `reviewDecision` is deliberately **not** a
   trigger: `CHANGES_REQUESTED` stays latched until re-review, so using it
   would re-trigger forever on an already-addressed round. Only
   timestamped events trigger.
4. Otherwise → no-op; the task keeps waiting for the human merge click.

Rate cost: N `pr-open` tasks × 6 passes/hour — a dozen open PRs is ~72
requests/hour against a 5,000/hour limit. Worst-case detection latency is
one pass interval (~10 min).

Poll failures (gh error, rate limit) log a warning and leave the task
untouched — the next pass retries, the standard failure posture.

## Done path (merge detected)

In order, all idempotent so a mid-sequence gh failure is retried next pass
(a merged PR still reads as merged):

1. Set the board status field to **Done**
   (`status_done_option_id`, new per-target config).
2. Tear down: end any leftover tmux session, remove the worktree
   (`git worktree remove --force` via the workspace module), delete the
   remote branch best-effort (`gh api -X DELETE .../git/refs/heads/<branch>`;
   ignore failure — repos with auto-delete-on-merge already removed it).
3. Save stage `done` with `done_at = now`; append event `merged`; Telegram
   notify (`task_done` template: title, PR URL).

**Flush.** Each pass deletes state files of `done` tasks whose `done_at` is
older than `done_retention_days` (new global config, default 7). The card
disappears from the console; the durable record remains in GitHub (merged
PR, closed issue, board item) and the event log.

## Reopen path (new feedback)

Two decoupled steps, so detection is never blocked by capacity:

**Detect (always):** set `feedback_pending = true`, event `pr-feedback`,
Telegram notify. Idempotent across passes — the flag (not the cursor)
suppresses re-detection, so already-pending feedback doesn't re-notify.

**React (gated):** on any pass where the task has `feedback_pending` and
capacity, budget, and a free slot allow (same gates as claiming new work):
allocate a slot, advance `feedback_cursor` to now (the spawn moment),
spawn an `address-review` session in the existing worktree with the new
prompt, clear `feedback_pending`. If gates deny, the task stays `pr-open`
with the badge showing; retried next pass.

**Cursor semantics.** The cursor is only ever written at spawn time; empty
means "any human feedback counts" (correct for a fresh PR — the implement
transition never sets it). Comments posted while a rework session ran were
visible to it live (the prompt reads the PR threads at run time), but
being after the cursor they conservatively re-trigger one more round
rather than risk missing feedback; a redundant round on a mid-rework
comment is the accepted cost of never losing one.

## Prompt: `prompts/address_review.md`

Same contract as the other stage prompts (`$issue_number`, `$repo`,
`$branch`, plus `$pr_number`). Instructs the session to:

1. Fetch the PR's unresolved review threads and comments via `gh`.
2. Address each point on branch `$branch`: code changes TDD-style per repo
   conventions; disagreements answered in the thread with reasoning, not
   silently ignored.
3. Re-run the full verification ladder from the implement prompt,
   including the E2E dispatch and `awaiting-ci` park
   (`{"stage": "address-review", "status": "awaiting-ci", "run_id": …}`).
4. Push, reply to the review threads describing what changed.
5. Write `{"stage": "address-review", "status": "done", "note": "<summary>"}`
   and exit.

`blocked` and stall handling work as in any stage (existing park
machinery, unchanged).

## Console

- Column `pr-open` retitled **"PR review"** — a PR awaiting human review or
  merge. Cards there may carry a **feedback-queued badge** (from
  `feedback_pending`, exposed on `TaskCard` like `park_note_pending`):
  "feedback queued — waiting for a free slot".
- `address-review` maps to the existing **In progress** column.
- New terminal column **Done** appended after `pr-open` in `COLUMNS`;
  stage `done` maps to it. Cards show merge time; they age out via the
  flush.

## Config

- Per target: `status_done_option_id` (single-select option id of the
  board's Done status), alongside the existing ready/in-progress ids.
  Documented in `targets.example.yaml`.
- Global: `done_retention_days: int = 7`.

## Error handling summary

| failure | behavior |
|---|---|
| `gh pr view` fails | warn, retry next pass |
| PR number unresolvable | warn each pass, task stays `pr-open` |
| board Done update fails | done path aborts, retried next pass (idempotent) |
| branch delete fails | ignored (best-effort) |
| spawn gates denied | `feedback_pending` badge, retry next pass |
| `address-review` session crashes | existing `HandleCrash` path (fails task, releases claim) |

## Testing

- **machine.py** (pure): `address-review` transitions — `done` →
  `PR_OPEN`, `awaiting-ci` → `ParkForCI`, `blocked` → `ParkForInput`,
  crash → `HandleCrash`; `done`/`pr-open` remain out of the in-flight set.
- **Poll classification** (pure function over the `gh pr view` payload):
  merged / closed / new-feedback / quiet; self- and bot-author exclusion;
  cursor comparison; latched `CHANGES_REQUESTED` does not re-trigger.
- **read_model**: column mapping for the two new stages, `feedback_pending`
  on the card, Done column present and ordered last.
- **main loop** (fake GitHub client, tmpdir state): merge → board Done +
  teardown + `done` stage; feedback with free capacity → spawn; feedback
  with full capacity → pending badge, spawn on a later pass; flush deletes
  only `done` tasks older than retention; poll failure leaves state
  untouched.
- **Frontend**: badge renders on a `feedback_pending` card; Done column
  renders.
