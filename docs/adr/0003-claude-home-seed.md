# 0003 — Box Claude config is a versioned seed in this repo, not a workstation export

Date: 2026-07-23
Status: accepted

## Context

Box sessions need Claude "global" config — process skills (superpowers:
brainstorming, writing-plans, executing-plans), conventions, guardrails —
which today exists only on the operator's workstation as a mix of plugin
state, personal skills, hooks, and CLAUDE.md. A naive export is wrong:
several workstation hooks directly contradict box behavior (the git
guardrail blocks pushes, but the implement stage must push and open PRs;
the rtk and npm hooks reference tools absent from the session image).
Additionally, `~/.claude` inside the container is a runtime mount
(claude-home), so the Containerfile cannot bake config into it.

## Decision

1. **Authored, not exported.** The box's Claude config is written for the
   box in `provision/claude-home/` (the *seed*): a minimal `settings.json`
   (`enabledPlugins` only), a box-specific `CLAUDE.md` (commit conventions,
   TDD, stage discipline), `skills/`, and a box-variant git guardrail hook
   (allows branch pushes, blocks `--force` and pushes to main — branch
   protection on target repos remains the primary enforcement).

2. **Converged with scoped authority.** The updater syncs seed →
   claude-home with full authority inside seed-managed paths
   (`settings.json`, `CLAUDE.md`, `skills/`, `hooks/`), including
   delete-propagation, and never touches machine state: `.credentials.json`,
   `projects/` transcripts (park/resume's `--continue` depends on them), and
   the plugin cache.

3. **Plugins installed by the updater, latest-or-pinned.** Plugin state is
   opaque and machine-managed, so it is not versioned in git; the updater
   runs `claude plugin install`/uninstall keyed off the seed's
   `enabledPlugins`, which supports declaring either a pinned version or
   latest per plugin.

4. **Two config layers per session.** Global layer: the claude-home mount,
   identical for every session. Repo layer: whatever the target repo's
   worktree checkout carries (its CLAUDE.md, committed skills, and
   skills-cli-synced repo skills — the target repo's responsibility, synced
   during worktree setup). Process skills never ride target repos; repo
   skills never ride the seed.

5. **The box is memoryless.** No engram/MCP memory in sessions: stage
   amnesia is what forces spec and plan artifacts to be complete. The only
   cross-task memory channel is committed docs and filed issues. Only specs
   are committed; plans stay worktree-local (`.agent/plan.md`) and are
   deleted by the dispatcher when the implement stage finishes.

## Considered options

- Export the workstation `~/.claude` — rejected: contradictory hooks,
  unreviewable, couples box behavior to a personal machine.
- Bake skills into the session image — rejected: the claude-home mount
  shadows `~/.claude` at runtime; image rebuild per config tweak.
- Distribute process skills via `@jesdi/skills-cli` per target repo —
  rejected: forces every target repo to carry pipeline-process concerns.
- Vendor superpowers skills as plain files in the seed — fallback only if
  CLI version pinning proves impossible and tracking latest is deemed
  unacceptable.
