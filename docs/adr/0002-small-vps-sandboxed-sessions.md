# 0002 — Small dedicated VPS, sandboxed sessions, CI-delegated E2E

Date: 2026-07-21
Status: accepted
Supersedes: the placement and session-model decisions of ADR 0001
(dedicated CX32, bare tmux sessions, host-Docker E2E). ADR 0001's
convergence, pull-based deploy, user-units, and box-local-targets
decisions stand unchanged — see cross-references below.

## Context

The Hetzner CX32 named in ADR 0001 is no longer available at the
planned price. A dedicated VPS was acquired instead: **2 vCPU / 4 GB,
Ubuntu 24.04**. This forced a reassessment of the session model and E2E
strategy. Requirements that could not be relaxed:

- Claude sessions run in a local sandbox with no root-equivalent access
  (the docker-group gap of ADR 0001 is deleted, not carried forward).
- E2E capability is kept; on-box nested containers fit only on paper
  at 4 GB (peak ≈ 3.5–4+ GB with session + runner fighting for 2 vCPU).
- A single capacity slot must not stall on a waiting session.

Two interim variants were explored on 2026-07-20 and discarded before
implementation:

- **Shared prod VPS (OVH VPS-2)**: firewall models conflict
  (Tailscale-only agent-ops vs. public-ingress portfolio_eval);
  docker-group root-equivalence would be on a machine running prod;
  OOM/disk blast radius merges with the live application. Rejected.
- **Dedicated 8 GB box with nested-podman E2E**: on-box E2E at
  capacity 1 with a nested container stack proved fragile — timing
  races, overlapping resource spikes — and the larger SKU costs more
  without delivering better isolation. Rejected.

## Decision

1. **Rootless Podman container per session.** No Docker daemon on the
   box; `agent` is in no privileged group (docker-group gap from ADR
   0001 is closed). Each task runs in `podman run -it` with user
   namespaces — container root maps to nothing on the host. Per-task
   caps: `--memory 2g --cpus 2`. The `agent-ops-session` image
   (node + Claude Code CLI, git, gh, python/pipenv, pnpm) is built from
   the repo's `Containerfile`; the updater rebuilds on `Containerfile`
   change, tags by content, never touches running containers mid-task.
   tmux wraps the container for TTY persistence (`/attach`,
   reply-injection); tmux session and container die and are recreated
   together at park/resume.

2. **E2E delegated to GitHub Actions** (`workflow_dispatch` on
   `portfolio_eval/e2e.yml`). The session's `make e2e-remote` wrapper
   dispatches and captures the run ID; the session then ends its turn and
   parks as `awaiting-ci`. The dispatcher polls completion via
   `gh run view`. Free-plan budget: 2,000 Actions minutes/month ≈
   130–250 E2E runs/month; a $0 spending limit makes quota exhaustion
   fail loudly rather than hang. E2E artifacts use `retention-days: 3`.

3. **Park/resume — one mechanism, two triggers.** Common mechanics:
   park records the Claude session ID, stops the container, marks the
   task state file, frees the slot; woken tasks are placed at the head
   of the queue; resume creates a fresh tmux+podman on the same worktree
   with `claude --resume <session-id>`, restoring full context from the
   host-mounted `~/agent-ops-state/claude-home` transcripts. Triggers:
   (a) human-input park — stop hook fires mid-stage → Telegram message
   with last output → wake on Telegram reply or `/attach`; (b) CI park —
   session writes `{status: awaiting-ci, run_id}` and stops → wake on
   run completion, dispatcher injects the CI verdict.

4. **`capacity: 1` as the operating point.** Memory budget: OS +
   Tailscale + dispatcher ≈ 0.5 GB; one session capped at 2 GB;
   ~1.5 GB headroom. The capacity-2 experiment is a sanctioned,
   reversible box-local edit (no merge cycle): set `capacity: 2`,
   per-session `--memory 1500m`; zram (≈ 2 GB, enabled at bootstrap)
   absorbs RSS spikes. Try when the queue is regularly deep while the
   slot is park-heavy; retreat on session OOM kills or visibly slow
   stages.

5. **Telegram inbound via `getUpdates` short-poll**, processed each
   dispatcher pass. No webhook, no ingress. Single private chat; the
   message is the session handle — replying to a park notification
   (Telegram's built-in reply) maps the answer to that task by
   replied-to message ID. `/attach <N>` holds for terminal attach;
   `/status` renders slots, running/parked/awaiting-ci tasks, and queue
   depth. This is the only status surface.

## Consequences

- The docker-group root-equivalence gap logged in ADR 0001 is closed.
  Container isolation (user namespaces, cgroup limits) replaces it.
- E2E moves off-box entirely, deleting all nested-container machinery
  and the E2E-slot logic from the dispatcher. Cost: CI round-trip
  latency (the session parks while the run is in flight — minutes to
  hours depending on queue). Accepted.
- Park/resume means the single slot is rarely wasted on blocking waits.
  A woken task may still wait behind a running task at capacity 1;
  the capacity-2 experiment is the escape valve.
- **Security invariant (ADR 0001, unchanged):** credentials held on the
  box must never have write access to the agent-ops repo. **New
  invariant:** the session's gh PAT gains `actions: write` on target
  repos (e.g., portfolio_eval) for `gh workflow run` — it must not have
  `actions: write` on jesdi/agent-ops.
- Pull-based convergence, user units, and box-local `targets.yaml`
  (ADR 0001 decisions 2–4) are unchanged. The updater gains one
  addition: `podman build` when `Containerfile` changes.
