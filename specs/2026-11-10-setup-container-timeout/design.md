# Setup-container timeout stops the container — Design

## Decisions

One line each: the decision and its tradeoff.

- **Bound the container twice: host-side `podman rm -f` on timeout, and podman's own `--timeout`
  on the run** — two mechanisms for one bound, but `rm -f` frees the name the moment the host gives
  up (the reproduction), while `--timeout` is the only bound that holds when the dispatcher dies
  mid-setup. Both are the issue's own fix options.
- **One value: a module constant for the provisioning timeout (1800 s) in `dispatcher/containers.py`**,
  read by `setup_cmd` for `--timeout` and by `create_workspace` for the `_sh` timeout — a constant,
  not a parameter or config (non-goal: configurable), so the two can never drift apart.
- **`--timeout` goes among the `podman run` options, before the image** — after the image it would
  be an argument of `sh -c`, not a podman option.
- **The removal runs only in a `TimeoutExpired` handler in `create_workspace`, then re-raises the
  original exception with a bare `raise`** — a non-zero exit (`CalledProcessError`) means the client
  ran to completion and `--rm` already removed the container, so no removal there.
- **The removal goes through `_sh` with no `log`, a 60 s timeout, and its `SubprocessError`/`OSError`
  suppressed** — `_sh` is the seam the existing tests fake; no `log` keeps the partial output in
  `.agent/setup.log`; 60 s matches `Sessions.end`'s `podman rm -f`; a failed removal is silent here
  and surfaces later as "name already in use" in the retry's setup log, which the operator already
  sees in the provisioning failure report.
- **Removal targets the exact name passed to `setup_cmd` (`task-<repo>-<issue>-setup`), built once**
  — the issue's `task-<N>-setup` predates the rekey; building it once keeps run and rm on one name.
- **No red-team of a data model** — the change has no table, no persisted state and widens no key.

## Data model

None. No table, file format or persisted state changes.

| Table | Field | Constraint | Why |
|---|---|---|---|
| — | — | — | — |

## Identities

None. The container name `task-<repo>-<issue>-setup` is unchanged; the removal reuses it.

## Seams

- `dispatcher.workspace.create_workspace(repository, issue)` — existing. Driven with the real
  `_sh`/`subprocess` path and the fake `podman` on `PATH` of
  `tests/test_setup_timeout_acceptance.py` (registry of live names, "name already in use" on
  reuse), the provisioning timeout shrunk from 1800 s to 1 s by wrapping `_sh`.
- `dispatcher.containers.setup_cmd(name, worktree, setup, clone)` — existing; signature unchanged,
  asserted on its argv.
