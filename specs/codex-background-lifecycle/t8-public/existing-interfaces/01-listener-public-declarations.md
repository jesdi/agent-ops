# Existing listener lifetime and readiness

This supplement declares existing interfaces. T8 implementation and genuine native
actual-launch verification remain required; settled behavior is in the
[presentation contract](../../runtime-presentation-contract.md).

Module/executable: `dispatcher.waitd`; `python -m dispatcher.waitd` has no command-line arguments. Its configuration is `AGENT_OPS_STATE_DIR`, falling back to `$HOME/agent-ops-state`. Explicit callable arguments select the same owned state directory and socket pathname.

```python
def sock_path(state_dir: str | Path) -> Path:
    ...

def serve(path: str | Path, state_dir: str | Path) -> None:
    ...

def main() -> None:
    ...
```

`sock_path(state_dir)` is `<state_dir>/wait/wait.sock`. `serve` is blocking and returns no server handle; there is no public stop method or health route. Its supported external lifetime is its owning process. Stop/restart refers to that exact owned process and the same owned state directory, not a deployed listener. Startup creates the wait directory and host credential and replaces an existing socket at the selected pathname. The host credential artifact is `<state_dir>/runtime-host-token` (host-only); its contents must not be published or mounted into a task.

Readiness observables are the owned process remaining live and the selected Unix HTTP endpoint answering an existing public request with a valid HTTP/JSON response. A pathname alone is not readiness. Existing `POST /runtime/view` takes a JSON object with `target`, `issue`, and optional `launch_id`; without a launch selector it requires the existing host header. An absent task may validly return JSON null. `RuntimeClient.view` reads the snapshot artifact and is not a socket liveness probe. The restart observable is a new socket inode at the same directory/path; this declaration does not add a readiness route or a shutdown callback. Runtime mutation/claim authorization stays exactly as declared in the existing contracts.

## Existing bound-client HTTP rejection

`BoundClient.event(binding, event)` retains the ordinary bound-client transport
contract: a non-200 HTTP reply raises `RuntimeError`. This includes the required
403 response to a container presentation claim. Denial does not return a boolean
claim result. Wire status and absence of mutation remain observable independently.
This declaration preserves existing client behavior and adds no exception or retry
policy to presentation.
