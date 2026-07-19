# Provisioning

`bootstrap.sh` sets up a fresh Ubuntu 24.04 Hetzner box. Dedicated to
agent-ops — deliberately NOT shared with the portfolio_eval prod VPS
(OVH VPS-2), despite that box having been sized for agent sessions:
isolation of firewall models, docker-group root-equivalence, and OOM/disk
blast radius won the trade-off. Size: CX32 (4 vCPU / 8 GB); E2E slots are
the sizing driver — capacity 3 does not fit in 4 GB. Revisit after
rollout step 5.
Ingress is Tailscale-only (UFW deny-all on the public interface).

All agent-ops units are systemd USER units under the `agent` user
(~/.config/systemd/user/, `loginctl enable-linger agent`): dispatcher,
waitd, keepalive, digest, update. Root is used only at bootstrap
(packages, ufw, tailscale, user + linger); convergence never runs as
root. Known gap: `agent` is in the docker group (root-equivalent) until
E2E moves to rootless Docker — logged as a hardening follow-up.

Deploys are pull-based convergence: an `agent-ops-update.timer` on the box
(every ~1 min; a fetch that finds nothing is one cheap roundtrip)
does `git pull --ff-only` against `main`, reinstalls the package if deps
changed, syncs systemd units, and restarts changed services. Updater and
dispatcher pass share a flock so code never swaps mid-pass; running
Claude sessions in tmux are unaffected by updates. Merging to
main (CI-gated) IS the deploy — no CI→VPS push, no GitHub-held
credentials, consistent with the spec's "GitHub is the state store; the
VPS is the compute" rule. Initial bootstrap stays a one-time manual run
(tailscale up, claude/gh logins, op token are interactive).

Runtime state lives in ~/agent-ops-state, outside the repo. That includes
the live targets.yaml: the repo ships targets.example.yaml only; the real
one (project field/option IDs, capacity) is box-local at
~/agent-ops-state/targets.yaml — deliberately outside CI so ops knobs are
tunable without a merge cycle, and so the repo checkout stays clean for
ff-only pulls.

Rollout order (from the design spec):
1. bootstrap.sh + the manual follow-ups it prints
2. dispatcher --dry-run against the live board
3. single-lane live run (capacity: 1) on one small `auto` task
4. companion PR to portfolio_eval: make e2e-slot + worktree settings allowlist
5. raise capacity to 3; measure VPS sizing under concurrent E2E
