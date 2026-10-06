# Openspec pipeline: one review of spec and plan, one implement session — Design

## Decisions

One line each: the decision and its tradeoff.

- **The plan session always reports "ready for review"; the dispatcher alone decides to skip
  the gate.** — A session can never skip the gate by what it signals; the cost is that the
  three skip conditions live in the dispatcher and not in the prompt.
- **A plan `done` is accepted only from the plan review gate.** — A `done` that arrives while
  the task is still in the plan stage is bounced back to the session once, then parks; a
  session that forgets the protocol costs one resume instead of an unreviewed implement.
- **The open-question count is cross-checked and fails closed.** — The plan signal carries the
  count; a missing, negative or non-integer count, or one that differs from the number of
  entries in the summary's open-questions section, means "the gate applies". A miscount on a
  gate-free track costs one unnecessary review, never a skipped one. (Forced by red-team 1.)
- **"A questionnaire was raised" is recorded by the dispatcher, not reported by the session.**
  — The dispatcher sets a flag on the task when a spec-stage or plan-stage session parks for
  answers; the flag survives a restart of the session. The session's own memory of it is not trusted.
- **An answer is never an approval.** — The plan prompt treats a reply that answers open
  questions or asks for changes as feedback: apply, push, wait again. Only an explicit approval
  ends the gate. One extra round trip when the operator meant both. (Forced by red-team 3.)
- **The gate keeps the spec gate's grace clock, config key and park.** — `spec_review_grace_minutes`
  keeps its name although it now times the plan review; renaming it would break every
  `targets.yaml` for no behaviour change.
- **The gate stage gets a new name; the old one stops being valid.** — `awaiting-plan-review`
  says what the operator reviews. A task file that still carries the old stage cannot be read,
  which is why the migration rewrites it before the dispatcher runs again.
- **The gate publishes the spec folder, and shows a summary from `.agent/`.** — The dispatcher's
  publish backstop commits and pushes the whole folder and links to it; the ticket list, the
  open questions and the stage-1 corrections are one Markdown summary the console serves as the
  task's request. The tickets stay uncommitted, so GitHub never shows them.
- **`spec_path` stays one file: the folder's `spec.md`.** — Later stages find `proposal.md` and
  `design.md` beside it. One path on the task instead of three; the folder is its parent.
- **Implement is one stage start, with no ticket cursor.** — The dispatcher no longer knows
  which ticket is in work; it shows the session's own progress note. The per-ticket gate cap
  does not apply to implement; `implement-spec`'s own four-round limit rules.
- **A retired task field is dropped by the loader, not rejected.** — A task file written before
  this change still carries the ticket cursor; the loader removes it as it already does for two
  earlier retired fields. Without this every old file, open pull requests included, would be
  skipped as corrupt. (Forced by red-team 2.)
- **A dead implement session fails the task as any dead session does.** — No automatic respawn:
  the operator resumes it and the fresh session reads the ledger. A long session dies more
  often than a short one, so a task can sit failed overnight. (Red-team 5, accepted.)
- **The implement prompt points at the skill file instead of invoking the skill.** —
  `implement-spec` cannot be invoked by a model; reading the file works on both runtimes. Two
  overrides only: no pull request, and the ledger and notes live in `.agent/`.
- **The review session writes the pull request description to a file before it removes
  anything.** — `.agent/pr-body.md` first, then the ADRs and `CONTEXT.md`, then the removal of
  `proposal.md` and `design.md`, then the pull request. A session that dies in between leaves a
  body file the next session uses. (Forced by red-team 4.)
- **The change-log rule lives in the one prompt part every stage already gets.** — It is added
  to the shared artifacts text, which also loses its rule to commit review copies under
  `docs/review/`. One place to edit; the cost is that the text is not stage-specific.
- **The migration is a command that edits task files directly and holds the pass lock.** — It
  reads raw JSON because the loader rejects the old stage and request kind; it takes the same
  lock a dispatcher pass takes, so the two never write a task file at once; a second run
  changes nothing. It is deleted after the deploy.
- **The migration leaves tasks in plan, implement and review alone and lists them.** — The
  operator drains those before deploying; a task found there is reported, not converted.

## Data model

Task files, stage signals and track config are JSON and YAML read by Python; there is no
database. "Constraint" is what the loader or the state machine enforces.

| Table | Field | Constraint | Why |
|---|---|---|---|
| Task | `stage` | one of the stage values; `awaiting-spec-review` removed, `awaiting-plan-review` added | the gate moved behind stage 2 |
| Task | `asked` (new) | boolean, default false; set only by the dispatcher when a spec-stage or plan-stage session parks for answers; never cleared | condition 2 of the gate skip |
| Task | `ticket_cursor` | retired; dropped on load | no per-ticket sessions |
| Task | `ticket_count` | integer ≥ 1 once the plan is valid | implement-start message and progress |
| Task | `gate_rounds` | not advanced by an implement session | the skill owns that loop |
| Task | `operator_request` | kind `plan-approval` with a non-empty path inside the worktree, or `answers`, or none; `spec-approval` removed | what the console shows at the gate |
| Stage signal (plan) | `open_questions` (new) | integer ≥ 0, equal to the entries of the summary's open-questions section; anything else reads as "gate applies" | condition 3 of the gate skip |
| Stage signal (plan) | `artifact` | the summary path under `.agent/` | the gate's request content |
| Stage signal (implement) | `note` | free text, shown as progress | "N/M tickets merged" |
| Track config | `plan_review` (new) | boolean, default true; any other type fails config load, naming the track | condition 1 of the gate skip |
| Worktree file | `.agent/plan-review.md` | written by the plan session before it signals; has an open-questions section | gate summary |
| Worktree file | `.agent/ledger.md` | written by the implement session; never committed | resume, and rulings for the pull request |
| Worktree file | `.agent/pr-body.md` | written by the review session before any removal | survives a review-session restart |

## Identities

None. The change widens no key: a task is still `target` plus `issue`.

## Seams

Where tests drive through. Each is existing unless marked new.

- `dispatcher.main.run_pass(cfg, deps)` — existing. A full pass over fake dependencies: a test
  sets a task file and a stage signal, runs a pass, and observes the task's new stage, the
  sessions started (with the prompt each received), the notifications sent and the publish
  calls. Drives every stage transition and every prompt requirement of this spec.
- `dispatcher.models.parse_policy(raw)` — existing. The track config loader.
- `GET /api/task/{target}/{issue}` — existing. The console's task view.
- `python -m dispatcher.openspec_migration <state_dir>` — new. The one-time migration command.
