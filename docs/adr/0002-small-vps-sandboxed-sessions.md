# 0002 — Small dedicated VPS, sandboxed sessions, CI-delegated E2E

Date: 2026-07-21
Status: accepted
Supersedes: the placement and session-model decisions of ADR 0001
(dedicated CX32, bare tmux sessions, host-Docker E2E). ADR 0001's
convergence, pull-based deploy, user-units, and box-local-targets
decisions stand unchanged (see Consequences).

## Context

The Hetzner CX32 named in ADR 0001 is no longer available at the planned
price. A dedicated VPS was acquired instead: **2 vCPU / 4 GB, Ubuntu 24.04**.
Requirements that could not be relaxed:

- Claude sessions run in a local sandbox with no root-equivalent access
  (the docker-group gap of ADR 0001 is deleted, not carried forward).
- E2E capability is kept; nested containers fit only on paper at 4 GB
  (peak ≈ 3.5–4+ GB with session + runner fighting for 2 vCPU).
- A single capacity slot must not stall on a waiting session.

Two variants were explored on 2026-07-20 and discarded:

- **Shared prod VPS (OVH VPS-2)**: firewall model conflicts; docker-group
  root-equivalence on a machine running prod; OOM/disk blast radius merges
  with the live application. Rejected.
- **Dedicated 8 GB box with nested-podman E2E**: timing races, overlapping
  resource spikes; larger SKU costs more without better isolation. Rejected.

## Decision

1. **Rootless Podman per session.** No Docker daemon; `agent` is in no
   privileged group. Each task: `podman run -it --memory 2g --cpus 2` with
   user namespaces (container root maps to nothing on the host). The
   `agent-ops-session` image (node + Claude Code CLI, git, gh, python/pipenv,
   pnpm) is built from `Containerfile`; the updater rebuilds on change, tags
   by content, never touches running containers. tmux wraps each container for
   TTY persistence; both die and are recreated together at park/resume.

2. **E2E delegated to GitHub Actions** (`workflow_dispatch` on
   `portfolio_eval/e2e.yml`). `make e2e-remote` dispatches and captures the
   run ID; the session parks as `awaiting-ci`; the dispatcher polls via
   `gh run view`. Free-plan budget: 2,000 Actions minutes/month ≈ 130–250
   runs/month; a $0 spending limit makes exhaustion fail loudly. Artifacts:
   `retention-days: 3`.

3. **Park/resume — one mechanism, two triggers.** Park records session ID,
   stops the container, marks state, frees the slot; woken tasks go
   head-of-queue; resume creates fresh tmux+podman on the same worktree with
   `claude --resume <session-id>` from host-mounted
   `~/agent-ops-state/claude-home` transcripts. Triggers: (a) human-input —
   Telegram message with last output → wake on reply or `/attach`; (b) CI —
   session writes `{status: awaiting-ci, run_id}` → wake on completion,
   dispatcher injects the verdict.

4. **`capacity: 1` as the operating point.** Budget: OS + Tailscale +
   dispatcher ≈ 0.5 GB; one session at 2 GB; ~1.5 GB headroom. Capacity-2
   experiment (box-local edit, no merge cycle): `capacity: 2`, per-session
   `--memory 1500m`; zram (≈ 2 GB, enabled at bootstrap) absorbs RSS spikes.
   Try when the queue is deep while the slot is park-heavy; retreat on OOM
   kills or visibly slow stages.

5. **Telegram inbound via `getUpdates` short-poll**, each dispatcher pass.
   No webhook, no ingress. Single private chat; the message is the session
   handle — replying to a park notification maps the answer to that task by
   replied-to message ID. `/attach <N>` for terminal attach; `/status` renders
   slots, tasks, and queue depth.

## Consequences

- Docker-group root-equivalence gap closed; user namespaces + cgroup limits replace it.
- E2E moves off-box, deleting nested-container machinery and E2E-slot logic; CI
  round-trip latency while the session parks. Accepted.
- Park/resume keeps the slot from wasting on blocking waits; capacity-2 is the escape valve.
- **Security invariants:** box credentials never write jesdi/agent-ops (ADR 0001,
  unchanged); the session's gh PAT gains `actions: write` on target repos
  (e.g., portfolio_eval) for `gh workflow run` but must not have it on jesdi/agent-ops.
- Pull-based convergence, user units, box-local `targets.yaml` (ADR 0001 decisions 2–4)
  unchanged; updater gains `podman build` when `Containerfile` changes.
