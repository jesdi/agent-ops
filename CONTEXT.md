# agent-ops

Unattended agent operations: a dispatcher on a small dedicated VPS picks tasks
from a GitHub Project board and drives them through staged, sandboxed Claude
sessions to a finished PR.

## Language

**Box**:
The dedicated VPS that runs the dispatcher and all sessions. There is exactly one.
_Avoid_: server, host, VPS (in prose — "box" everywhere)

**Session**:
One `podman run … claude` invocation in a herdr tab, working a single stage of
one task. Sessions are disposable; state lives in artifacts and claude-home.
_Avoid_: agent (that's the OS user), container (that's the isolation layer only)

**Stage**:
One step of a task's lifecycle (spec → plan → implement, once per ticket →
review → pr-open → address-review), each executed by a fresh session whose
only input is the previous stage's artifact.
_Avoid_: phase, step

**Ticket**:
One vertical slice of the implementation, produced by the plan stage as
`.agent/tickets/NN-slug.md` and worked by exactly one implement session.
Never committed; the numeric prefix is the execution order.
_Avoid_: plan (the plan stage now produces tickets, not a plan file), task
(that is the whole issue)

**Review stage**:
The session that reads only the issue, the spec, the tickets and the diff —
never a summary — fixes what it finds, rebases onto main, runs the gates
and end to end, and opens the PR.
_Avoid_: PR stage, verify stage

**Gate**:
The target repository's own green check (`gate_cmd` in targets.yaml —
tests, lint, CRAP), run by the session after every ticket and in review.
End to end (`verify_cmd`) is separate and runs once, off-box.
_Avoid_: verification ladder

**Loop cap**:
The configured number of rounds a bounded loop (review fixes, gate fixes,
e2e fixes, CI fixes on an open PR) may run before the task parks for the
operator. Parks, never fails.
_Avoid_: retry limit (that is the plan-format retry)

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
Allowance minus used on a window. The gate's actual input; the **binding
window** is the one with the least headroom.

**Claude-home**:
The box-side persistent Claude config directory (`~/agent-ops-state/claude-home`),
mounted at `/root/.claude` inside every session. It is the box's "global"
Claude configuration and transcript store.
_Avoid_: dotclaude, global config (ambiguous with the mac's)

**Claude-home seed**:
The versioned, declarative source of claude-home's config, authored in the
private agent-ops-infra repo (`provision/claude-home/`) and converged onto the box by the
updater. Credentials and transcripts are never part of the seed.
_Avoid_: export, config copy (the seed is authored for the box, not exported
from a workstation)

**Spec**:
The design artifact produced by the spec stage. Draft committed and pushed
before human review; approved before planning. Other registered Markdown
review artifacts are also committed to the task branch.

**Repo skills**:
Skills scoped to a target repo, declared in that repo's `.my-skills.json` and
synced by `@jesdi/skills-cli`. Distinct from process skills.

**Process skills**:
Repo-agnostic workflow skills (to-spec, to-questionnaire, to-tickets,
prototype, wizard, tdd, code-review, deep-quality-review…) that stage
prompts invoke. Vendored as files into the claude-home seed from the skills
repo (`make vendor-skills`); never a plugin, never carried by target repos.

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

**Reset causes** describe logical work boundaries (new stage/ticket, operator intervention, new PR
cycle). A replacement process, model switch, or subscription reset is **not** by itself a fresh
fix-loop allowance.

**Execution admission** (question 3) is `dispatcher/usage.py::admits(model)`:
a verdict per `provider/model` from the provider's windows, built once per
pass and asked at every spawn site. Usage collectors are `usage_providers.py`
adapters, one per provider, fetched only for providers the model policy
references. Loop policy stays independent of all of it: waiting for
headroom does not spend a fix round, and a denial for one provider never
prevents considering another. Still deferred: runtime adapters (running a
session on a non-Anthropic provider), the router that picks among admitted
models by task type, cross-runtime session continuation.

## Operator-request ownership

**Durable spec reference**: `TaskState.spec_path` — recorded when the task enters AWAITING-SPEC-REVIEW,
retained across plan / implement / review / PR-OPEN / address-review. Worktree-relative path.

**Operator request** = `TaskState.operator_request`: `None` (no request), `{"kind": "spec-approval"}`,
or `{"kind": "answers", "path": "<worktree-relative path>"}`. The dispatcher owns its lifecycle writes:

- Establish on entering AWAITING-SPEC-REVIEW (spec-approval).
- Set answers kind when a valid answers artifact is signalled.
- Clear on: successful resume, stage transition, ordinary+exhaustion park, terminal stage, CI/login supersede.
- Retain on admission denial (resources unavailable at wake time).
- Re-arm on a resumed gate re-signal.

The web layer READS `operator_request`; it never infers a request from park state.

**Endpoint and UI**: one `/api/task/{target}/{issue}/request` → `OperatorRequest | null` with a
discriminated `readable | unavailable` content union. One `RequestPanel` + media renderer in the
frontend. Approval is only possible on a `readable` spec-approval request.

**Legacy conversion**: localized in `state._read` (migrated from the old `artifact`/`spec_path`-inferred
request). The external stage-signal artifact parser (`read_stage_signal`) is separate and retained.

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
Done-card flush; archive entries do not consume capacity or return to the board.

`TaskState.terminal_at` records the first transition into done, failed or
canceled. State serialization preserves that timestamp across unrelated
terminal writes and clears it on reopening. Collection cancels expiry for
active or parked tasks. Each dispatcher pass removes stored content 30 days
after the terminal transition; metadata, GitHub links and archived task context
remain. Task state owns archive serialization and normalizes legacy terminal
timestamps on read. `Sources` owns active-or-archived lookup and artifact
delivery resolution; web routes do not read the artifact filesystem directly. Failed-task worktrees remain available for autopsy under the existing
workspace policy; artifact cleanup only owns its stored copies.
