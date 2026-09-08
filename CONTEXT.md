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

**Claude-home**:
The box-side persistent Claude config directory (`~/agent-ops-state/claude-home`),
mounted at `/root/.claude` inside every session. It is the box's "global"
Claude configuration and transcript store.
_Avoid_: dotclaude, global config (ambiguous with the mac's)

**Claude-home seed**:
The versioned, declarative source of claude-home's config, authored in the
agent-ops repo (`provision/claude-home/`) and converged onto the box by the
updater. Credentials and transcripts are never part of the seed.
_Avoid_: export, config copy (the seed is authored for the box, not exported
from a workstation)

**Spec**:
The design artifact produced by the spec stage. The only committed stage
artifact — approved by a human, then committed to the task branch.

**Repo skills**:
Skills scoped to a target repo, declared in that repo's `.my-skills.json` and
synced by `@jesdi/skills-cli`. Distinct from process skills.

**Process skills**:
Repo-agnostic workflow skills (to-spec, to-questionnaire, to-tickets,
prototype, wizard, tdd, code-review, deep-quality-review…) that stage
prompts invoke. Vendored as files into the claude-home seed from the skills
repo (`make vendor-skills`); never a plugin, never carried by target repos.

## Flagged ambiguities

- "Global skills" — ambiguous between the mac's `~/.claude` and the box's
  claude-home. The two are separate, independently authored configurations;
  say **mac config** or **claude-home seed**.

## Example dialogue

— "The spec stage failed: it couldn't find the to-questionnaire skill."
— "Then the claude-home seed is missing a process skill. Add it to
  `provision/skills-pins.json`, run `make vendor-skills`, merge, and the updater converges the box; don't
  install anything on the box by hand."
— "Should I also add it to portfolio_eval's `.my-skills.json`?"
— "No — that's for repo skills. Process skills never ride target repos."
