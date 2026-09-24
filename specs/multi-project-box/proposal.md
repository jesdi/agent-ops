# Multi-project box

## Why

The box runs only `portfolio_eval` today. The operator wants to run several projects at once
(starting with a factorial test repo tomorrow), filing GitHub issues on each project's board and
letting the box work them unattended. The dispatcher already loops over a `targets:` list, but a
second target would break it:

- `capacity` is counted per target while slots are global. Two targets at `capacity: 3` could each
  claim 3, and the 8 GB box would OOM at 5 × 1500m.
- Targets are served in list order. The first target's new claims take every free unit before the
  second target's woken tasks (approved specs, CI fixes) resume.
- A target without an off-box e2e run or a worktree provisioning script has to fake
  `verify_cmd` / `setup_cmd`, and a fake `verify_cmd` would report e2e as passed.
- Specs waiting for review auto-park after 15 minutes, with no way to turn that off.
- Telegram messages do not say which project an issue belongs to.

## For whom

The operator (one person) running several projects on the single box and steering them from
Telegram and the web console.

## Goal

Two or more targets share the box safely and fairly. Concurrency never exceeds one box-wide
`capacity`, in-flight work across all targets goes before new claims, and a repo can be onboarded
with no e2e or provisioning script.

## Non-goals

- **Console project filter** (dropdown, `?project=`, box-wide meters). This is the next slice, a
  separate spec.
- **Default ranking without `rank_cmd`** (Boost field, then board position). A later slice. Until
  then every target keeps a `rank_cmd`.
- **agent-ops (or agent-ops-infra) as a target.** This is blocked by the ADR 0001 invariant until a
  machine GitHub account and a required approving review on `main` exist.
- **`target#N` in Telegram commands.** Bare numbers stay, and the existing refusal covers
  cross-target collisions.
- **Per-target spec-review grace.** `null` is box-wide only.
- **Weighted shares between targets.** Only fewest-active-first plus an optional per-target cap.
- **Auto-approving specs.** The spec-review gate stays for every target.
- **Onboarding automation.** Cloning, adding the repo to the PAT, and filling in Project field IDs
  stay a manual runbook.
