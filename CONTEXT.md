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
One step of a task's lifecycle (spec → plan → implement), each executed by a
fresh session whose only input is the previous stage's committed artifact.
_Avoid_: phase, step

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

**Plan**:
The implementation artifact produced by the plan stage. Never committed: it
lives in the worktree (`.agent/plan.md`) and is deleted by the dispatcher when
the implement stage finishes.

**Repo skills**:
Skills scoped to a target repo, declared in that repo's `.my-skills.json` and
synced by `@jesdi/skills-cli`. Distinct from process skills.

**Process skills**:
Repo-agnostic workflow skills (brainstorming, writing-plans, executing-plans,
TDD…) that stage prompts rely on. Delivered box-side via the claude-home seed,
not via target repos.

## Flagged ambiguities

- "Global skills" — ambiguous between the mac's `~/.claude` and the box's
  claude-home. The two are separate, independently authored configurations;
  say **mac config** or **claude-home seed**.

## Example dialogue

— "The spec stage failed: it couldn't find the brainstorming skill."
— "Then the claude-home seed is missing a process skill. Add it to
  `provision/claude-home/`, merge, and the updater converges the box; don't
  install anything on the box by hand."
— "Should I also add it to portfolio_eval's `.my-skills.json`?"
— "No — that's for repo skills. Process skills never ride target repos."
