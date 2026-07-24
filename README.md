# agent-ops

Autonomous backlog pipeline dispatcher. It drains a GitHub-hosted backlog by
running autonomous Claude Code sessions on a small dedicated VPS, and reports
progress over Telegram.

The guiding rule: **GitHub is the state store; the VPS is the compute.** There
is no CI→VPS RPC. The box converges to `main` by pulling; merging (CI-gated)
*is* the deploy.

## How it works

- **Dispatcher** (`dispatcher/`) — polls the backlog, selects the next task
  within a budget/capacity limit, provisions a git worktree, and launches a
  Claude Code session for it.
- **Sessions** run in rootless Podman containers (the `agent-ops-session`
  image: Node + Claude Code CLI, git, gh, Python/pipenv, pnpm), one per task,
  each wrapped in a tmux session for TTY persistence and reply injection.
- **Park / resume** — when a session needs human input or is waiting on a CI
  run, the dispatcher stops the container, frees the slot, and resumes via
  `claude --continue <message>` when the wake event fires (a Telegram reply or
  CI completion). Woken tasks go to the head of the queue.
- **Telegram** (`telegram/`) — outbound notifications and digests, plus
  inbound replies that feed back into parked sessions.
- **Pull-based convergence** — an `agent-ops-update.timer` on the box does
  `git pull --ff-only` against `main` (~every minute), reinstalls the package
  when deps change, syncs systemd units, restarts changed services, and
  rebuilds the session image when the `Containerfile` changes.

See [`docs/adr/`](docs/adr/) for the architecture decisions and
[`provision/README.md`](provision/README.md) for the deployment model.

## Layout

| Path            | What lives there                                              |
| --------------- | ------------------------------------------------------------ |
| `dispatcher/`   | Backlog polling, task selection, budget, sessions, state     |
| `telegram/`     | Outbound notifications/digests and inbound reply handling    |
| `provision/`    | `bootstrap.sh`, systemd units, and the pull-based updater    |
| `docs/adr/`     | Architecture decision records                                |
| `tests/`        | pytest suite                                                 |
| `Containerfile` | The per-session sandbox image                                |
| `targets.example.yaml` | Template for the box-local `targets.yaml` (capacity, thresholds, model policy) |

## Development

Requires Python ≥ 3.11.

```bash
pip install -e '.[dev]'
pytest
```

## Deployment

agent-ops runs on a dedicated Hetzner VPS (2 vCPU / 4 GB, Ubuntu 24.04) with
Tailscale-only ingress (UFW deny-all on the public interface). All runtime
units are systemd **user** units under the `agent` user; convergence never runs
as root. Provisioning and rollout are documented in
[`provision/README.md`](provision/README.md).

## License

[MIT](LICENSE) © 2026 jesdi
