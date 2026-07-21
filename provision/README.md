# Provisioning

`bootstrap.sh` sets up a fresh Ubuntu 24.04 dedicated VPS for agent-ops.
Box size: **2 vCPU / 4 GB** — see ADR 0002. Deliberately NOT shared with
the portfolio_eval prod VPS (OVH VPS-2): firewall model conflicts,
container isolation requirements, and OOM/disk blast radius won the
trade-off (ADR 0001). Ingress is Tailscale-only (UFW deny-all on the
public interface).

All agent-ops units are systemd USER units under the `agent` user
(~/.config/systemd/user/, `loginctl enable-linger agent`): dispatcher,
waitd, keepalive, digest, update. Root is used only at bootstrap
(packages, UFW, Tailscale, user + linger); convergence never runs as
root. `agent` is in no privileged group — the docker-group gap from
ADR 0001 is closed (Podman, not Docker, is used; no Docker daemon exists
on the box).

Bootstrap installs **Podman** and tmux; enables **zram swap (~2 GB)** for
the memory cushion that absorbs session RSS spikes and makes the
capacity-2 experiment survivable.

Deploys are pull-based convergence: an `agent-ops-update.timer` on the box
(every ~1 min) does `git pull --ff-only` against `main`, reinstalls the
package if deps changed, syncs systemd units, and restarts changed
services. The updater also rebuilds the `agent-ops-session` Podman image
when the `Containerfile` changes (tagged by content; running containers are
never touched mid-task). Updater and dispatcher pass share a flock so code
never swaps mid-pass. Merging to main (CI-gated) IS the deploy — no
CI→VPS push, no GitHub-held deploy credentials.

Claude sessions run in rootless Podman containers: the `agent-ops-session`
image (node + Claude Code CLI, git, gh, python/pipenv, pnpm) is launched
per task with the task worktree and `~/agent-ops-state/claude-home`
(Claude auth + transcripts) mounted; `--memory 2g --cpus 2`. A tmux
session wraps each container for TTY persistence and reply-injection. E2E
runs off-box on GitHub Actions (`e2e.yml` in portfolio_eval, dispatched via
`gh workflow run`); there is no nested-container machinery on the box.

Park/resume: when a session needs human input or is waiting on a CI run,
the dispatcher stops the container, frees the slot, and resumes via
`claude --resume` on the same host-mounted transcripts when the wake event
fires (Telegram reply or CI completion). Woken tasks are always
head-of-queue.

Runtime state lives in ~/agent-ops-state, outside the repo. That includes
the live targets.yaml: the repo ships targets.example.yaml only; the real
one is box-local — deliberately outside CI so ops knobs (capacity,
thresholds) are tunable without a merge cycle and the checkout stays clean
for ff-only pulls.

Rollout order (from the design spec §9):
1. bootstrap.sh + the manual follow-ups it prints
2. dispatcher --dry-run against the live board
3. single live task (capacity: 1) on one small `auto` task
4. park/resume drill — trigger a human-input park, reply from Telegram,
   verify context and reply arrive correctly
5. E2E drill — companion e2e.yml merged into portfolio_eval; dispatch a
   real run, verify awaiting-ci park → CI completion → resume with verdict
6. steady state
7. capacity-2 experiment (optional, box-local edit): set `capacity: 2`,
   per-session `--memory 1500m`; retreat on OOM kills or slow stages
