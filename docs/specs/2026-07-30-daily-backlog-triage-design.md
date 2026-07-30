# Daily backlog triage

Every morning at 07:30 CET the box triages the backlog of every repo it
manages — all `targets.yaml` targets plus `infra_repo` — through one
headless session per repo. Triage consumes a real capacity unit, so it can
never overwhelm the box.

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

### Per-repo sweep

Repos = every target in `targets.yaml` plus `infra_repo`, deduped.
Sequential, one at a time:

1. Read the repo's cursor from `state_dir/triage_cursors.json`. First run
   seeds the cursor to "now" and triages nothing (backfill by hand-editing
   the file; no `--triage-full` flag until a need appears).
2. Run the pre-fetch script. Zero issues in the window → skip the repo,
   no session, no tokens.
3. Spawn one headless session (see below) with a ~20-minute wall-clock
   kill.
4. On success, advance the cursor to this run's start time. On failure,
   leave it, so tomorrow retries the same window.

Accepted quirk: triage actions bump `updated_at`, so a touched issue
reappears in the next day's window once as a no-op, then falls out.

**Budget gate:** before the sweep, check usage the way the dispatcher
does; past `budget_threshold`, skip the whole run and say so in the
Telegram message.

**Model:** new optional `triage_model` config key, defaulting to the
`models.default` of targets.yaml.

## Pre-fetch script — `provision/triage-prefetch.sh`

Runs on the box host (where `gh` is authenticated), once per repo:
`triage-prefetch.sh OWNER/REPO CURSOR` → one JSON blob on stdout, embedded
in the session prompt. Deterministic, mutation-free, testable standalone.

Contents:

- Issues created or updated since the cursor (PRs excluded): number,
  title, body, labels, author, comments (truncated per comment to bound
  the prompt).
- Label inventory with descriptions.
- Issue types available to the repo.
- The full open-issue list as number+title pairs — duplicate detection
  across the whole backlog happens in-context.

## Session

`podman run --rm` (no `-it`), session image, claude-home mounted, gh
config `:ro`, main clone mounted `:ro` at its host path as cwd, and
`state_dir/triage` mounted read-write for the report file:
`claude -p "<prompt>" --permission-mode auto --model <triage_model>`.

## Prompt — `prompts/triage.md`

Versioned next to the stage prompts. Core instructions:

> You are an issue triage agent. Analyze the issue title and description,
> gather relevant repository context, and take only the triage actions
> supported by the evidence.
>
> - Always check the issue types, labels, and fields before selecting
>   values.
> - Search for similar issues and distinguish duplicates from merely
>   related issues.
> - Update type, labels, fields, or assignment via `gh` when the issue
>   content supports the change. If the evidence does not support a
>   change, leave that metadata unchanged.
> - For obvious spam or gibberish, record a close-as-not-planned
>   suggestion in the report. Do not close it yourself.
> - For a duplicate, record a close-as-duplicate suggestion naming the
>   matching issue. Do not close it yourself, and do not suggest closing
>   merely related issues.
> - If the task is trivial or similar to tasks automated previously,
>   apply the `auto` label. If substantial fog of war remains that would
>   need heavy human interaction, apply `human-required`.
> - If the issue is incomplete, comment asking the author for the
>   specific missing information and avoid unsupported triage actions.
> - Do not post routine triage report comments. Comment only when
>   communicating with the issue author is necessary.

Deltas from the operator's draft, decided in brainstorming:

- **No Copilot clause.** `auto` is the "suitable for an agent" marker.
- **Closes are suggestions only** (spam/duplicates land in the report for
  one-tap human confirmation). Labels, type, fields, and author-facing
  comments are applied directly.
- **`auto` is label-only.** Triage never promotes a card to Ready; the
  human moves work in front of the dispatcher.

Appended to the prompt: the pre-fetched JSON blob, the instruction to use
`gh` for anything beyond it, and the report contract below.

**Report contract:** the session ends by writing a JSON report — actions
taken, suggested closes with reasons, issues skipped — to a mounted
report directory: `state_dir/triage/<repo>-<date>.json`. File-based
because parsing agent stdout is fragile.

## Reporting

After the sweep (or its expiry), one Telegram message via the existing
notifier: per repo — labels applied, comments posted, suggested closes as
issue links — plus any repo whose session failed or produced no report,
and the skipped/budget-gated cases. A failed repo never blocks the
others.

## Testing

Pytest, subprocess (gh/podman) stubbed, matching existing dispatcher
tests:

- enqueue: `--triage` writes the request and exits without spawning
- pass logic: pending request pauses new claims; slot claim, release,
  2-hour expiry
- cursor read/seed/advance; cursor untouched on failure and expiry
- repo dedupe (`infra_repo` also a target)
- skip-on-empty window; budget gate
- failure isolation between repos; report aggregation; Telegram rendering

Pre-fetch script: smoke test against a stubbed `gh` on PATH.
