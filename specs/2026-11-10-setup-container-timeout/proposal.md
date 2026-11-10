# Setup-container timeout stops the container

## Why

`create_workspace` (`dispatcher/workspace.py`) runs the repository's `setup_cmd` in a one-shot
container built by `containers.setup_cmd` (`podman run --rm --name task-<repo>-<issue>-setup …`)
through `_sh(…, timeout=1800)`. On timeout, `subprocess.run` SIGKILLs its child, which is the
foreground **podman client**, not the container. SIGKILL cannot be caught, so the client never
proxies it to the container: the container keeps running, and `--rm` never fires because the
client that would remove it is dead. Setup containers deliberately carry no `--memory`/`--cpus`,
so a hung setup (a wedged package install, `sleep infinity`) runs uncapped and forever. Nothing
removes it later: `Sessions.end` only removes the session containers `task-<repo>-<issue>` and the
legacy `task-<issue>`, and the provisioning failure path
(`_report_provisioning_failure`) only files a report and quarantines the task. When the operator
lifts the quarantine, the retry's `podman run --name task-<repo>-<issue>-setup` fails with
"name already in use", which reads as a second provisioning failure. Root cause: the timeout was
placed at the host `_sh` level, which bounds the client process, and nothing bounds or cleans up
the container itself. Reproduced by `tests/test_setup_timeout_acceptance.py` (strict xfail until
fixed).

## For whom

The operator of the agent-ops box: a hung provisioning run must cost one failure report, not an
unbounded container eating the box's memory and CPU and blocking every retry of that task.

## Goal

When a setup run exceeds the provisioning timeout, its container is stopped and removed and its
name is free for a retry, and a setup container never outlives the provisioning timeout even when
the dispatcher itself dies mid-setup.

## Non-goals

- Resource caps (`--memory`/`--cpus`) on setup containers: they stay uncapped by design.
- Changing the 1800 s value or making it configurable.
- Sweeping `task-*-setup` containers orphaned before this fix: a one-time manual
  `podman rm -f` on the box.
- Cleanup on non-timeout failures: a non-zero exit means podman ran to completion and `--rm`
  already removed the container.
- The issue's ride-along polish, already absorbed by the code (see spec.md, "What moved").
