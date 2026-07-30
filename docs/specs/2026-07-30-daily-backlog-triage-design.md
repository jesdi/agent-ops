# Daily backlog triage

Every morning at 07:30 CET the box triages the backlog of every repo it
manages — all `targets.yaml` targets plus `infra_repo` — through one
headless session per repo. Triage consumes a real capacity unit, so it can
never overwhelm the box. The session only *decides*; deterministic code
fetches its input and applies its output.

## Trigger

New systemd pair in `provision/`: `agent-ops-triage.timer` /
`agent-ops-triage.service`, cloned from the digest pattern.

- `OnCalendar=*-*-* 07:30:00 Europe/Madrid` (systemd calendar timezones
  track CET/CEST through DST), `Persistent=true`.
- Oneshot service: `python -m dispatcher.main --config
  %h/agent-ops-state/targets.yaml --triage`, wrapped in the same `op run`
  environment as the digest unit.
- Converged onto the box by the updater like every other unit.

`--triage` is a thin enqueue: it writes a pending triage request (stamped
with the request time) into the state dir and exits. It spawns nothing.

## Dispatcher execution

All execution happens inside normal dispatcher passes.

1. **Priority.** While a pending triage request exists, the dispatcher
   claims no new board tasks. Running sessions are never preempted; triage
   waits for a natural release. Consequence, accepted: on a fully busy
   morning, Ready-queue tasks queue behind triage for up to 2 hours.
2. **Claim.** When a capacity unit is free, the dispatcher claims it for
   triage and runs the per-repo sweep sequentially inside that one slot —
   one headless session per repo, standard `session_memory` /
   `session_cpus` caps, so capacity accounting stays uniform.
3. **Acquisition timeout.** If no unit frees within 2 hours of the request
   timestamp, the request expires. The Telegram report says "triage
   skipped — no capacity within 2 h"; no cursor moves, so tomorrow's
   window covers today too. The timeout gates only slot *acquisition*:
   once the sweep starts it runs to completion.
4. **Release.** The slot is released when the last repo finishes, and the
   dispatcher resumes claiming board tasks.

### Per-repo sweep — decide between two deterministic halves

Repos = every target in `targets.yaml` plus `infra_repo`, deduped.
Sequential, one at a time:

1. **Cursor.** Read the repo's cursor from
   `state_dir/triage_cursors.json`. First run seeds the cursor to "now"
   and triages nothing (backfill by hand-editing the file; no
   `--triage-full` flag until a need appears).
2. **Pre-fetch** (`dispatcher/triage_prefetch.py`, deterministic). Zero
   issues in the window → skip the repo, no session, no tokens.
3. **Decide.** Spawn one read-only headless session (see below) with a
   ~20-minute wall-clock kill. Its sole output is a decisions JSON.
4. **Apply** (`dispatcher/triage_apply.py`, deterministic). Validate and
   execute the decisions on the box host.
5. On success, advance the cursor to this run's start time. On failure at
   any step, leave it, so tomorrow retries the same window.

Accepted quirk: applied actions bump `updated_at`, so a touched issue
reappears in the next day's window once as a no-op, then falls out.

**Budget gate:** before the sweep, check usage the way the dispatcher
does; past `budget_threshold`, skip the whole run and say so in the
Telegram message.

**Model:** new optional `triage_model` config key, defaulting to the
`models.default` of targets.yaml.

## Pre-fetch — `dispatcher/triage_prefetch.py`

Runs on the box host (where `gh` is authenticated), once per repo.
Deterministic, mutation-free, pytest-covered (subprocess stubbed) —
Python rather than sh because it assembles a multi-source JSON blob with
per-comment truncation, and the repo's precedent for exactly this shape
is Python (`rank.py`, `meta.py`).

Output, one JSON blob embedded in the session prompt:

- Issues created or updated since the cursor (PRs excluded): number,
  title, body, labels, author, comments (truncated per comment to bound
  the prompt).
- Label inventory with descriptions.
- Issue types available to the repo.
- The full open-issue list as number+title pairs — duplicate detection
  across the whole backlog happens in-context.

## Session — read-only

`podman run --rm` (no `-it`), session image, claude-home mounted, gh
config `:ro`, main clone mounted `:ro` at its host path as cwd, and
`state_dir/triage` mounted read-write for the decisions file:
`claude -p "<prompt>" --permission-mode auto --model <triage_model>`.

The session mutates nothing on GitHub. `gh` is available read-only for
digging (viewing a suspected duplicate, reading linked code); every
change it wants goes into the decisions JSON.

## Prompt — `prompts/triage.md`

Versioned next to the stage prompts. Core instructions:

> You are an issue triage agent. For each issue in the provided batch,
> analyze the title, body, and comments, gather repository context where
> needed (read-only `gh` and the mounted clone are available), and record
> only the triage decisions supported by the evidence in the decisions
> file. You change nothing directly.
>
> - Use only labels present in the provided inventory — never invent
>   labels. At most one type label (`bug`, `enhancement`,
>   `documentation`, `question`); at most two area labels. When unsure,
>   choose fewer.
> - Check suspected duplicates against the provided open-issue list;
>   confirm with `gh issue view` before recording a close-as-duplicate
>   suggestion naming the matching issue. Do not suggest closing merely
>   related issues.
> - For obvious spam or gibberish, record a close-as-not-planned
>   suggestion.
> - If the issue is trivial or similar to tasks automated previously,
>   record the `auto` label. If substantial fog of war remains that would
>   need heavy human interaction, record `human-required`.
> - If the issue is incomplete, draft a comment asking the author for the
>   specific missing information, and record no other decisions for it.
> - Never draft routine triage-report comments; a comment exists only to
>   ask the author something.
> - If the evidence does not support a change, record nothing for that
>   issue.

Deltas from the operator's draft, decided in brainstorming and review:

- **No Copilot clause.** `auto` is the "suitable for an agent" marker.
- **No assignment, no board fields.** Nobody to assign; Impact/Effort
  scoring stays with the backlog skill's interactive `triage` verb.
  `auto` is label-only — promotion to Ready stays human.
- **Decide-only.** All `gh`-mutation mechanics left the prompt; the
  contract is the decisions JSON.
- **Taxonomy inline.** The backlog skill's label discipline (one type
  max, 0–2 areas, inventory-only) is stated, not implied — model routing
  in targets.yaml keys off these labels, so loose labeling corrupts
  routing.

Appended to the prompt: the pre-fetched JSON blob and the decisions-file
contract (path `state_dir/triage/<repo>-<date>.json`; per issue: labels
to add/remove, type, comment draft, close suggestion with reason, or
explicit skip).

## Apply — `dispatcher/triage_apply.py`

Runs on the box host after the session exits. Deterministic,
pytest-covered:

- Validates every decision: labels must exist in the pre-fetched
  inventory, at most one type label, at most two area labels. Invalid
  decisions are **rejected and reported**, never posted.
- Applies label/type changes and posts author comments via `gh`.
- **Never closes.** Close suggestions pass through to the Telegram report
  for one-tap human confirmation.
- Emits the per-repo action summary consumed by the report.

The session having no write path plus apply-side validation means an
agent misjudgment can at worst mislabel within the taxonomy — it cannot
invent labels, close issues, or touch the board.

## Reporting

After the sweep (or its expiry), one Telegram message via the existing
notifier: per repo — labels applied, comments posted, suggested closes as
issue links, rejected decisions — plus any repo whose session failed or
produced no decisions file, and the skipped/budget-gated cases. A failed
repo never blocks the others.

## Prerequisite — taxonomy extension

Add `auto` and `human-required` to the backlog skill's label taxonomy and
its `setup` provisioning (they are new orthogonal pipeline markers, not
type or area labels), so every repo has them before the first sweep.
Without this the validator would reject every `auto`/`human-required`
decision as an invented label.

## Testing

Pytest, subprocess (gh/podman) stubbed, matching existing dispatcher
tests:

- enqueue: `--triage` writes the request and exits without spawning
- pass logic: pending request pauses new claims; slot claim, release,
  2-hour expiry
- cursor read/seed/advance; cursor untouched on failure and expiry
- repo dedupe (`infra_repo` also a target)
- skip-on-empty window; budget gate
- pre-fetch: window filtering, comment truncation, blob shape
- apply: taxonomy validation (invented label rejected, type-label cap),
  label/comment application, closes never executed, rejection reporting
- failure isolation between repos; report aggregation; Telegram rendering
