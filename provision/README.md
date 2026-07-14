# Provisioning

`bootstrap.sh` sets up a fresh Ubuntu 24.04 Hetzner box (CX22-class to
start; E2E slots are the sizing driver — revisit after rollout step 5).
Ingress is Tailscale-only (UFW deny-all on the public interface). Deploys
of this repo are `git pull` in ~/agent-ops. Runtime state lives in
~/agent-ops-state, outside the repo.

Rollout order (from the design spec):
1. bootstrap.sh + the manual follow-ups it prints
2. dispatcher --dry-run against the live board
3. single-lane live run (capacity: 1) on one small `auto` task
4. companion PR to portfolio_eval: make e2e-slot + worktree settings allowlist
5. raise capacity to 3; measure VPS sizing under concurrent E2E
