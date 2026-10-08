# agent-ops

Unattended agent operations: a dispatcher on a small dedicated VPS picks tasks
from a GitHub Project board and drives them through staged, sandboxed Claude
sessions to a finished PR.

## Language

**Box**:
The dedicated VPS that runs the dispatcher and all sessions. There is exactly one.
_Avoid_: server, host, VPS (in prose — "box" everywhere)

**Capacity**:
How many sessions the box runs at once, shared by every target. Each pass
spends it on work already in flight first, then claims new issues one unit at
a time: each unit goes to the target with the fewest active tasks that still
has a candidate and is under its `max_active`, ties to the one that claimed
least recently. Claims also stop at `max_open`, the box-wide cap on
unfinished tasks (parked and pr-open ones hold no capacity but still hold a
worktree).
_Avoid_: slots per target (there are none; `max_active` is only a cap)

**Session**:
One `podman run … claude` invocation in a herdr tab, working a single stage of
one task. Sessions are disposable; state lives in artifacts and claude-home.
_Avoid_: agent (that's the OS user), container (that's the isolation layer only)

**Stage**:
One step of a task's lifecycle (spec → plan → implement → review → pr-open →
address-review), each executed by a fresh session whose
only input is the previous stage's artifact. Spec: stage 1 of `to-openspec`
writes `proposal.md` and `spec.md`; no gate. Plan: checks that stage 1,
runs stage 2 (`design.md`, tickets), then waits at the plan review gate
unless the gate skip applies. Implement: one `implement-spec` session works
every ticket. Review: an independent review, then the content move and the
PR.
_Avoid_: phase, step

**Ticket**:
One vertical slice of the implementation, produced by the plan stage as
`.agent/tickets/NN-slug.md`. One implement session works every ticket of a
task. Never committed; the numeric prefix orders the tickets.
_Avoid_: plan (the plan stage now produces tickets, not a plan file), task
(that is the whole issue)

**Track**:
An operator-named kind of work (`trivial`, `standard`, `security`) defined in
`targets.yaml` by a prose `when` and, per stage, an ordered list of entries.
Triage picks the track for the spec stage (label `track:<name>`); the spec
session picks it for plan, implement and review and writes it in its signal.
_Avoid_: tier, profile, rule

**Pinned track**:
A track named in `models.pinned`. Required pace, the session-bound rule and a
provider-first priority mode do not apply to it: its entries are tried in the
written order. The usage gate, picks and one-shot overrides still apply; when
no entry of its list is admitted, the task waits. Triage and the tracks the
list does not name follow the mode.
_Avoid_: locked track, fixed track

**Entry**:
One element of a track's stage list: `provider/model[@effort]`.
_Avoid_: profile (a Claude Code term)

**Pick**:
The entry chosen when a task enters a stage, reused by every later session of
that stage. The implement pick is dropped when review starts. PR feedback has
one pick of its own, chosen by the first session that addresses PR feedback
and reused by every later round. A denied pick waits.

**Untracked**:
A candidate with no `track:` label; it specs on `models.untracked`.

**Review stage**:
The session that reads only the issue, the spec, the tickets and the diff —
never a summary — and fixes what it finds; reads the implement session's
ledger only after that review. It then writes the PR description to
`.agent/pr-body.md` (Why, Goal and Non-goals of `proposal.md`, Decisions of
`design.md`, open rulings of the ledger), adds an ADR under `docs/adr/` for
each decision that constrains later changes and each new term to this file,
removes `proposal.md` and `design.md` from the branch, rebases onto main,
runs the gates and end to end, and opens the PR from that file. The order
is a safety property: a session that dies part way leaves a body file the
next one uses.
_Avoid_: PR stage, verify stage

**Spec folder**:
`specs/<YYYY-MM-DD>-<topic>/` in the task's worktree, dated with the day the
spec session starts. It holds `proposal.md`, `spec.md` and `design.md` during
the task; `main` keeps only `spec.md`.
_Avoid_: design file, spec file (the spec is the folder's `spec.md`)

**Plan review gate**:
The one point where a task waits for the operator before a pull request
exists: after the plan session wrote the design and the tickets. The
operator is shown the spec folder on GitHub and the review page on the
console. Only an explicit approval starts implement.
_Avoid_: spec review gate, spec approval (retired with the old flow)

**Review page**:
`.agent/review.html`, written by the plan session with the `review-page` skill
before it reports ready: the ticket list, the open questions and the corrections
it made to stage 1. The dispatcher judges it by shape only: a regular file in the
worktree, at most 256 KiB, carrying the template marker. It is the request the
console shows at the gate. Never committed.

**Open question**:
What the plan session reports at its end: a red-team finding the design
leaves unresolved, a choice between two behaviours that differ for the user
or the business, or an input the design assumes and no source provides.
Shown on the review page, counted in the ready report.

**Gate-free track**:
A track whose `targets.yaml` entry carries `plan_review: false`. A track
without the key has the gate. Gate-free is one of the conditions of the gate
skip, never the only one.

**Ledger**:
`.agent/ledger.md` in the task's worktree: the rulings of the implement
session and the state of every ticket. A session that finds one continues
from it. The review session reads it only after its own review. Never
committed.

**Implement progress**:
The note of the implement session's last `working` report, for example
"2/4 tickets merged". The console shows it on the task page while the task
is in implement.

**Gate**:
The target repository's own green check (`gate_cmd` in targets.yaml —
tests, lint, CRAP), run by the implement session for every ticket and in review.
End to end (`verify_cmd`) is separate and runs once, off-box. A target with
no `verify_cmd` has no pre-PR e2e run; the PR's GitHub checks are the only
e2e signal.
_Avoid_: verification ladder

**Loop cap**:
The configured number of rounds a bounded loop (review fixes, gate fixes,
e2e fixes, CI fixes on an open PR) may run before the task parks for the
operator. Parks, never fails. A round an implement session names is never
counted, whatever the loop: the `implement-spec` skill owns its loops and
their limits.
_Avoid_: retry limit (that is the plan-format retry)

**Background wait**:
A live session that ended its turn mid-stage while background work it started
(a build, a gate run) is still running, reported by the Stop hook and recorded
by waitd as a background marker. The dispatcher neither parks it nor lets the
stall timer catch it. The wait is over once the session starts a new turn
(today's rules apply again), but the marker stays until a waiting ping or the
session's end, so the same work reported again keeps its clock. The cap clock
starts at the first report and restarts when a report names new work; past
`background_wait_seconds` (the cap) the task parks for the operator.
_Avoid_: stall (a stall is a session with no output and no reason to be quiet)

**Provider**:
A subscription whose usage windows the box spends (`anthropic`; later
`openai`, `nvidia`). Named by the `provider/` prefix of a model id; a bare
id is anthropic's. The same bare model under two providers spends two
different windows.
_Avoid_: vendor, backend, API

**Window**:
One limit a provider reports: a kind (session, weekly), an optional model
scope (Fable), a used fraction, when it resets, and its nominal length.
_Avoid_: quota, bucket

**Allowance**:
The fraction of a window the box may have consumed by now — the session
threshold, or the weekend-weighted elapsed share of the week plus the pace
margin.
_Avoid_: budget (that word now only names the stall marker and the pings)

**Headroom**:
Allowance minus used on a window. The gate admits a model while every
window it considers has headroom above zero.

**Required pace**:
Remaining quota of a weekly window (1 minus used) divided by its remaining
time (the share of the window still ahead, on the allowance's
weekend-weighted clock, no floor). 1.0× means spending the rest evenly lands
exactly on the reset; higher means quota that will go unspent unless the box
spends it faster. An entry's required pace is the lowest among the weekly
windows its model draws on. It orders the entries of a list (highest first,
entries without one last, ties as written); the gate never reads it.
_Avoid_: urgency, burn rate

**Session-bound**:
A provider whose unscoped weekly window has at least as much remaining quota
as its spendable maximum: its session week share (`session_week_share` in
`targets.yaml`, the share of the week one session spends when used up to the
session threshold) times the sessions left before that window resets. The
open session counts for the part of the threshold it has not used; after its
reset, every started 5 hours of real time counts as one. Even using every
session left it cannot spend its week, so its entries are tried first; the
gate never reads it. A provider with no session week share, no unscoped
weekly window or unavailable usage is never session-bound.

**Priority mode**:
The one box-wide choice of which provider's entries are tried first on every
list the box routes (the stage lists of every track, of every target's own
policy, and the triage list): `auto`, or the name of a provider. A provider's
name puts its entries first and the rest after, each group in written order,
and the session-bound rule is not applied; `auto` orders by session-bound,
then required pace. It only orders: the usage gate still decides what may
run, a stage's pick is kept, and a one-shot override wins. Stored in
`<state_dir>/provider-priority.json` (`dispatcher/priority.py`), read once
per dispatcher pass and once per triage sweep, so a change applies from the
next pass; status lines and the console also read it whenever they build an
order. Missing, unreadable, or naming a provider no routed list names,
it reads as `auto` and the file is left as it is.
_Avoid_: preferred provider, default provider

**Binding window**:
Of the windows a model draws on — unscoped ones plus any scoped window
whose display name matches the model — the one with the least headroom. It
decides the verdict and is what the stall note and the console name.

**Claude-home**:
The box-side persistent Claude config directory (`~/agent-ops-state/claude-home`),
mounted at `/root/.claude` inside every Claude session. It is the box's
"global" Claude configuration and transcript store. Its Codex sibling is
**Codex-home**.
_Avoid_: dotclaude, global config (ambiguous with the mac's)

**Runtime**:
The CLI a session runs for a provider — `claude` for `anthropic`, `codex`
for `openai`. One record per provider in `dispatcher/runtimes.py` owns the
CLI name, home mount, env, herdr agent name and the launch/resume
commands; nothing else branches on provider. The effort vocabulary stays in
`PROVIDER_EFFORTS` (`dispatcher/models.py`), which config validation reads.
_Avoid_: backend, driver, adapter (that is the usage adapter)

**Codex-home**:
Claude-home's sibling for Codex sessions (`~/agent-ops-state/codex-home`,
mounted at `/root/.codex`), converged from the **Codex-home seed**. Holds the
Codex login (`auth.json`), which only Codex itself refreshes — sessions, or
the codex-keepalive unit when none is live.
_Avoid_: dotcodex, Codex config (the seed is the config's source; this is
the live directory)

**Codex-home seed**:
The versioned, declarative source of codex-home's config (`config.toml`,
`AGENTS.md`, skills), authored in agent-ops-infra (`provision/codex-home/`)
and converged onto the box by the updater. Credentials and transcripts are
never part of the seed.
_Avoid_: codex export, config copy (as for the Claude-home seed)

**Claude-home seed**:
The versioned, declarative source of claude-home's config, authored in the
private agent-ops-infra repo (`provision/claude-home/`) and converged onto the box by the
updater. Credentials and transcripts are never part of the seed.
_Avoid_: export, config copy (the seed is authored for the box, not exported
from a workstation)

**Spec**:
`specs/<date>-<slug>/spec.md`: the requirements and scenarios of one change,
written by the spec stage with `proposal.md` beside it; the plan stage adds
`design.md`. The folder is on the task branch for the plan review gate. The
review stage removes `proposal.md` and `design.md`, so main keeps only
`spec.md` of a change. No copy of a questionnaire or its answers is
committed; they stay under `.agent/`.

**Change log**:
What `specs/` is: one folder per past change, as decided then, never brought
up to date. The code and this file are the present state; where a spec and
the code disagree, the code wins. Every stage prompt says so.
_Avoid_: living spec, source of truth (for `specs/`)

**Repo skills**:
Skills scoped to a target repo, declared in that repo's `.my-skills.json` and
synced by `@jesdi/skills-cli`. Distinct from process skills.

**Process skills**:
Repo-agnostic workflow skills that stage prompts invoke: `to-openspec`
(stage 1 in spec, stage 2 in plan, where it runs `red-team-data-model` and
`to-tickets`), `to-questionnaire`, `prototype` and `diagnosing-bugs` (spec),
`implement-spec` (implement, which owns `tdd` per ticket and its own
reviews), `review-diff` with `deep-quality-review` and
`resolving-merge-conflicts` (review). `to-spec` is not a box skill: it is the
Mac tool that writes the design into a `spec-ready` issue body. They live in
the claude-home seed (agent-ops-infra, ADR 0003): the jesdi ones pinned in
its `.my-skills.json` and installed with `@jesdi/skills-cli`, the mattpocock
ones vendored as files (`make vendor-skills`); never a plugin, never carried
by target repos.

## Loop-policy ownership

**Single owner**: `dispatcher/loops.py` owns all round accounting, cap arithmetic, and reset scopes.
`dispatcher/state.py` remains the serialisation owner; loops depends on state, never the reverse.
The dispatcher owns I/O and the one round-effect executor `_apply_loop_decision` (injected callable:
`park_exhausted`). Nothing outside `loops.py` may mutate `*_rounds` fields at runtime; defaults and
serialisation fields in `state.py` are exempt, as is read-only presentation of the counters.

**Three distinct questions** — kept separate by design:

1. *(Task state machine)* What work is next?
2. *(Loop policy)* Is another fix attempt allowed? — owned by `loops.py`.
3. *(Execution-admission policy)* Which suitable model/runtime has allowance, or should it
   wait?

A non-exhausted loop decision is eligibility to *retry*, not permission to launch. The usage gate
(admission) and capacity still decide when launch happens.

**Reset causes** describe logical work boundaries (new stage, operator intervention, new PR
cycle). A replacement process, model switch, or subscription reset is **not** by itself a fresh
fix-loop allowance.

**Execution admission** (question 3) is the `admit` callable each pass builds
from `dispatcher/usage.py::admits`: a verdict per `provider/model` from the
provider's windows, asked at every spawn site about the model that spawn
launches. Usage collectors are `usage_providers.py`
adapters, one per provider. The dispatcher fetches only the providers the
model policy references; the console also shows any other provider whose
adapter reads (Codex run by hand), and nothing admits on that reading. Loop policy stays independent of all of it: waiting for
headroom does not spend a fix round, and a denial for one provider never
prevents considering another. The router is `dispatcher/state.py::launch_entries`: the dispatcher launches the first admitted entry of the task track for the stage, tried in the order `dispatcher/priority.py::order` gives for the priority mode (a fixed mode: that provider's entries first; auto: a session-bound provider's entries first, then highest required pace); labels and board effort are not routing inputs. Each provider has a runtime (`dispatcher/runtimes.py`); a stage never
changes provider, so cross-runtime session continuation is excluded by rule,
not pending. See docs/specs/2026-09-24-codex-runtime-design.md.

A model-limited queue candidate or claimed task may carry a durable, one-shot
execution override. Queue claims and active stage transitions store it under
`execution-overrides/`; a parked task carries it through the existing resume
intent and `TaskState.resume_model_override` / `resume_bypass_usage`. The
requested model must belong to the target's configured policy. A wake that
names no model launches on a stored execution override; a wake that names
one wins, and the stored override is used up with that launch. The choice
survives ordinary capacity, slot, or provisioning denial and is consumed only
after the selected session starts. A usage bypass applies to that launch only;
it never bypasses box capacity or slot allocation.

## Operator-request ownership

**Durable spec reference**: `TaskState.spec_path` — the spec folder's `spec.md`, recorded when the spec
session reports done, retained across plan / AWAITING-PLAN-REVIEW / implement / review / PR-OPEN /
address-review. Worktree-relative path.

**Operator request** = `TaskState.operator_request`: `None` (no request),
`{"kind": "plan-approval", "path": "<worktree-relative path>"}` (the plan session's review page,
`.agent/review.html`), or `{"kind": "answers", "path": "<worktree-relative path>"}`. The dispatcher owns
its lifecycle writes:

- Establish on entering AWAITING-PLAN-REVIEW (plan-approval).
- Set answers kind when a valid answers artifact is signalled.
- Clear on: successful resume, stage transition, ordinary+exhaustion park, terminal stage, CI/login supersede.
- Retain on admission denial (resources unavailable at wake time).
- Clear when the session at the gate reports `working` (it reworks the plan on feedback).
- Clear when the plan signal is bounced (`_retry_stage`).
- A plan-approval request is bound to one plan revision: its `fingerprint` is a digest over the
  review page, the ticket files and the spec folder's `spec.md`, `proposal.md` and `design.md`, taken
  when it is armed (`artifacts.plan_revision`; not sent to the console). A ready report at the gate
  that names another revision or another page path clears the request and is a new review
  round; a request with no fingerprint matches no plan. Every file is read without following a
  symlink and without blocking, up to 1 MB: a file that cannot be read that way makes the revision
  unavailable, which clears the request and has the outcome of a failed ticket check.
- Re-arm on a ready report at the gate with no request armed for that plan: a new review round
  (ticket check, publish, notification, fresh grace clock). The grace clock runs for an armed
  request only. The ticket check runs on every ready report that is not for the armed revision.
- A task at the gate gets 2 unattended rounds (`unattended_rounds`, owned by `dispatcher/loops.py`:
  respawns of a dead session plus new rounds since the operator last acted; the round that answers
  an operator reply is not counted). The next round parks the task for review with the request
  armed; a dead session with the rounds used up parks it with a message that claims no ready plan.

**Gate skip**: the dispatcher alone skips the plan review gate (`machine._skips_gate`), on the first
ready report of a task, when all of these hold: the task's own track is gate-free (`plan_review: false`;
a track the ready report names does not count), `TaskState.asked` is false (neither the spec session
nor the plan session parked for answers), and the report's `open_questions` is the integer 0, its
`artifact` is `.agent/review.html`, and that page confirms the count (its number of
`data-q` question blocks is 0; `artifacts.count_open_questions`). A missing or malformed count or
page, or one that fails the shape check, means "the gate applies". The ticket check and
the spec folder publish run as at the gate, then implement starts in the same pass;
no review notification, no request. `TaskState.gated` is set at gate entry, and when a dead
gate session is respawned, and never cleared: a task that waited once never skips, also after a respawn
puts it back in the plan stage.

The web layer READS `operator_request`; it never infers a request from park state.

**Endpoint and UI**: one `/api/task/{target}/{issue}/request` → `OperatorRequest | null` with a
discriminated `readable | unavailable` content union. One `RequestPanel` + media renderer in the
frontend. Approval is only possible on a `readable` plan-approval request.

**Retired fields are dropped on load; nothing else is converted**: `state._read` drops the
retired task fields (`pending_reply`, `artifact`, `ticket_cursor`, `ticket_tracks`,
`ticket_names`, `ticket_without_pick`) and rejects the retired
`awaiting-spec-review` stage and `spec-approval` kind. `read_stage_signal` is the one parser of
the session-written `.agent/stage.json`, for the dispatcher and the console.

**Three distinct questions** — kept separate by design:

1. *(Task state machine)* What work is next?
2. *(Operator-request lifecycle)* Is there a pending operator action, and what content does it need?
3. *(Loop policy)* Is another fix attempt allowed? — owned by `loops.py`.

Request handling must remain independent of loop comparisons, reset logic, model IDs, and provider names.

## Flagged ambiguities

- "Global skills" — ambiguous between the mac's `~/.claude` and the box's
  claude-home. The two are separate, independently authored configurations;
  say **mac config** or **claude-home seed**.

## Example dialogue

— "The spec stage failed: it couldn't find the to-questionnaire skill."
— "Then the claude-home seed is missing a process skill. Add it to
  `provision/skills-pins.json` in agent-ops-infra, run `make vendor-skills`, merge, and the updater converges the box; don't
  install anything on the box by hand."
— "Should I also add it to portfolio_eval's `.my-skills.json`?"
— "No — that's for repo skills. Process skills never ride target repos."

## Task review artifacts

A **review artifact** is a named output used by a human to review the task or
by later sessions to guide the work: spec, prototype, diagram, questionnaire
and answers. Scratch files and logs are excluded.

Sessions maintain `.agent/artifacts.json` (`id`, `name`, worktree-relative
`path`) under the shared policy in `prompts/artifacts.md`. IDs and file paths
remain stable across revisions; entries survive stage and session changes.
HTML is self-contained. Sessions commit and push Markdown outside ignored
scratch directories; collection verifies the remote content before offering
a GitHub link. Spec publication retains its existing gate backstop.

`dispatcher/task_artifacts.py` owns collection, stored content, publication
references and cleanup. The store lives in `state_dir/artifacts/`, keyed by
both target and issue. The web reads it through authenticated artifact routes.
Each artifact has a stable route which redirects to GitHub or serves isolated
content. Generated HTML/SVG has an opaque sandbox origin and no API access.
After merge, GitHub destinations use the last verified published commit,
because the task branch is deleted. Archived task details survive the normal
finished-task flush (done, failed and won't do, `done_retention_days` after
`terminal_at`; it also removes the worktree and local branch); archive entries
do not consume capacity or return to the board.

`TaskState.terminal_at` records the first transition into done, failed or
canceled. State serialization preserves that timestamp across unrelated
terminal writes and clears it on reopening. Collection cancels expiry for
active or parked tasks. Each dispatcher pass removes stored content 7 days
after the terminal transition; metadata, GitHub links and archived task context
remain. Task state owns archive serialization and normalizes legacy terminal
timestamps on read. `Sources` owns active-or-archived lookup and artifact
delivery resolution; web routes do not read the artifact filesystem directly. Failed-task worktrees remain available for autopsy under the existing
workspace policy; artifact cleanup only owns its stored copies.
