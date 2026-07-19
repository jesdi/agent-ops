# 0001 — Dedicated agent VPS with pull-based convergence

Date: 2026-07-19
Status: accepted

## Context

agent-ops needs a host for the dispatcher and its autonomous Claude
sessions. The portfolio_eval production VPS (OVH VPS-2, PR #129) was
explicitly sized for "the app stack plus up to 3 concurrent Claude Code
agent sessions", so sharing it was a live option at zero marginal cost.
Separately, we wanted provisioning and configuration changes to flow
"through CI", while the dispatcher design rule says *GitHub is the state
store; the VPS is the compute — no CI→VPS RPC*, and the box's firewall
is Tailscale-only deny-all, unreachable by GitHub webhooks or hosted
runners.

## Decision

1. **Dedicated Hetzner CX32** (4 vCPU / 8 GB, Ubuntu 24.04) for
   agent-ops. Not shared with prod. CX22 rejected: `capacity: 3` with
   concurrent E2E slots does not fit in 4 GB.
2. **Pull-based convergence, tracking `main`**: an
   `agent-ops-update.timer` on the box (every ~1 min) does
   `git pull --ff-only`, reinstalls the package when deps change, syncs
   unit files, and restarts only changed units. Merging to `main` *is*
   the deploy. No CI→VPS push; a GitHub webhook was rejected because it
   requires public ingress. A CI wake-up ping over Tailscale remains a
   possible later bolt-on; the pull path stays the source of truth.
3. **All units are systemd user units** under the `agent` user with
   linger. Root is used only at bootstrap; convergence never runs as
   root. The updater and the dispatcher pass share a flock so code is
   never swapped mid-pass. Running Claude sessions (tmux) are unaffected
   by updates; new code takes effect at the next pass/stage boundary.
4. **Live `targets.yaml` is box-local** at `~/agent-ops-state/`; the
   repo ships `targets.example.yaml` only. Ops knobs (capacity,
   thresholds) are tunable without a merge cycle and the checkout stays
   clean for ff-only pulls.
5. **CI gates the deploy branch**: a minimal test workflow plus branch
   protection on `main` (PRs only, green checks required).

## Consequences

- Full isolation from prod: conflicting firewall models never merge;
  agent OOM/disk blowups cannot take the app down; the box can be
  rebuilt violently. Cost: ~€7/mo. PR #129's agent-session sizing
  headroom on the VPS-2 is freed back to prod.
- No deploy credentials live in GitHub, and no ingress is opened.
  Convergence latency is ~30 s average — dwarfed by the dispatcher's
  own 10-minute pass cadence.
- **Security invariant:** credentials held on the box (the agent's gh
  PAT, and any future per-target user tokens) must never have write
  access to the agent-ops repo — the box executes `main`, so a
  write-capable agent could deploy code to itself. Branch protection
  enforces this even if a credential is over-scoped.
- Target config changes are hand-edits on the box, outside review —
  accepted deliberately.
- In-flight tasks keep state written by the previous version, so state
  file schemas must stay backward-readable across merges.
- Known hardening gap: `agent` is in the `docker` group
  (root-equivalent locally) until E2E moves to rootless Docker.
