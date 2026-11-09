# agent-ops

## When the box misbehaves

Run `ssh box '~/.local/bin/agent-ops status'` first. It is a read-only report from local state: a `VERDICT` line (why the box is or isn't claiming), then rows for the dispatcher, units, projects, holds, slots, tasks, questions and errors. Then diagnose with `/diagnosing-bugs`.

## Map of the box

- SSH alias `box` is `agent@ubuntu-4gb-hel1-1`. Run everything as `agent`.
- State dir `~/agent-ops-state`:
  - `targets.yaml`: the config (projects, capacity, `paused`)
  - `pass.json`: heartbeat of the last dispatcher pass
  - `task-*.json`: one file per task
  - `quarantine/`: tasks held back by a blocker issue
  - `triage-request.json`: a pending triage sweep, which pauses claims
  - `budget-stalled`: usage budget spent, resets on its own
  - `auth-dark`: usage unknowable, the login is likely dead
- User units: `systemctl --user status 'agent-ops-*'`.
- Live sessions are herdr tabs named `task-<repo>-<issue>`.

## Rules

- Probes stay read-only.
- Ask the operator before restarting or changing anything. Restarting `agent-ops-herdr.service` kills every live session.
