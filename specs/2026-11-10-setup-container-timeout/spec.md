# Setup-container timeout stops the container — Spec

Issue #17. Seam: `dispatcher.workspace.create_workspace` (existing), driven with the real
`_sh`/`subprocess` path and a fake `podman` executable on `PATH` that behaves like the real
client: `run --name N` registers container `N` and blocks; killing the client leaves `N`
registered; `rm -f N` / `kill N` / `stop N` unregister it; `run` with an already registered name
fails with "name already in use" (exit 125). The reproduction lives in
`tests/test_setup_timeout_acceptance.py`; the test shrinks only the 1800 s provisioning timeout
to 1 s.

## Requirements

1. When the setup run exceeds the provisioning timeout, `create_workspace` force-removes the
   container named `task-<repo>-<issue>-setup` (`podman rm -f <name>`, its own short timeout)
   before the `subprocess.TimeoutExpired` propagates.
2. The `TimeoutExpired` still propagates unchanged to the caller (so `_report_provisioning_failure`
   reports and quarantines exactly as today), and the partial output is still written to
   `.agent/setup.log`, even when the removal itself fails or times out.
3. After a timed-out setup, a retry of `create_workspace` for the same task can start its setup
   container: the name is free.
4. The setup container's argv carries podman's own run timeout (`--timeout <seconds>`) equal to
   the host-side provisioning timeout, both taken from one value, so podman stops the container
   even when the dispatcher dies before it can remove it.
5. A setup run that finishes in time (exit 0 or non-zero) triggers no removal call.

## Scenarios

### Scenario: hung setup is stopped at the timeout (the reproduction)

- **Given** repository `portfolio_eval` with `setup_cmd` that never returns, and the provisioning
  timeout shrunk to 1 s
- **When** `create_workspace(repository, 42)` runs
- **Then** it raises `subprocess.TimeoutExpired`
- **And** no container named `task-portfolio_eval-42-setup` remains registered
  (today: it stays registered — the bug)

### Scenario: partial log survives the cleanup

- **Given** the setup of task 42 times out after printing `partial out` on stdout and
  `partial err` on stderr
- **When** `create_workspace` handles the timeout
- **Then** `.agent/setup.log` holds `partial out\npartial err\n`
- **And** `podman rm -f task-portfolio_eval-42-setup` was called

### Scenario: removal failure does not mask the timeout

- **Given** the setup of task 42 times out and `podman rm -f` exits 1 (or raises
  `TimeoutExpired` itself, or `podman` is missing: `OSError`)
- **When** `create_workspace` handles the timeout
- **Then** the original `subprocess.TimeoutExpired` for the `podman run` argv propagates, not the
  removal's error
- **And** `.agent/setup.log` still holds the partial output

### Scenario: retry after a timeout reuses the name

- **Given** task 42's setup timed out once and the task's worktree exists
- **When** `create_workspace(repository, 42)` runs again with a `setup_cmd` that now succeeds
- **Then** the setup container `task-portfolio_eval-42-setup` starts (no "name already in use")
  and `create_workspace` returns the worktree path

### Scenario: podman bounds the container itself

- **Given** repository `portfolio_eval` with `setup_cmd = scripts/setup-worktree.sh`
- **When** `containers.setup_cmd("task-portfolio_eval-42-setup", wt, …)` builds the argv
- **Then** it contains `--timeout` followed by `1800`, placed among the `podman run` options
  (before the image)
- **And** `create_workspace` passes the same 1800 as the host timeout of that `_sh` call

### Scenario: a setup that finishes needs no cleanup

- **Given** the setup of task 42 exits 0 (and, separately, exits 1 with `CalledProcessError`)
- **When** `create_workspace` runs
- **Then** no `podman rm`, `kill` or `stop` call is made, and the outcome is unchanged from today
  (returns the worktree; or raises `CalledProcessError` with the output in `.agent/setup.log`)

## What moved in the code underneath the issue

- The issue names branch `fix/container-setup-step`; that work is merged. The call is now
  `dispatcher/workspace.py` `create_workspace` → `_sh(containers.setup_cmd(f"task-{repository.name}-{issue}-setup", …), timeout=1800, log=…/setup.log)`.
  The container name includes the repository name (`task-<repo>-<issue>-setup`), not
  `task-<N>-setup`.
- Ride-along "`containers.clone_root` raises a bare IndexError": `clone_root` no longer exists;
  the clone path is the repository's configured `clone_path`, handed in by the caller. Nothing to do.
- Ride-along "`_sh`'s CalledProcessError hides captured stderr": `_sh` already writes stdout and
  stderr to `.agent/setup.log` on success, failure and timeout, and
  `_report_provisioning_failure` attaches `failures.setup_log_tail`. Nothing to do.

## Out of scope

- Resource caps on setup containers; changing or configuring the 1800 s value.
- Removing setup containers orphaned before this fix (one-time manual `podman rm -f`).
- Cleanup after a non-timeout setup failure (`--rm` already handles it).
