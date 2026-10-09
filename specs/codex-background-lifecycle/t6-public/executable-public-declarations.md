# Existing supervisor and listener executable declarations

Approved public executable declarations for isolated T6 acceptance and implementation.

The listener executable is `python -m dispatcher.waitd`. `AGENT_OPS_STATE_DIR`
selects an isolated state directory, and its Unix HTTP socket is
`<state>/wait/wait.sock`. The host `RuntimeClient` prepares and reads through that
listener and automatically supplies `X-Runtime-Host`; a fixture must not print
credential contents. Use short owned temporary directories, readiness checks and
bounded owned-process shutdown before deleting homes/state.

The supervisor executable is:

```text
python3 -P -m dispatcher.codex_supervisor --model MODEL --prompt-file PATH
    [--effort EFFORT] [--resume CONVERSATION_ID]
```

Its immutable environment selectors are `AGENT_OPS_TARGET`, `AGENT_OPS_ISSUE`,
`AGENT_OPS_LAUNCH_ID`, `AGENT_OPS_CONVERSATION_ID`, `AGENT_OPS_STAGE`,
`AGENT_OPS_TICKET`, and `AGENT_OPS_STATE_DIR`. Run it in the owned task worktree
with an explicit deployed source path and installed Python WebSocket dependency.
Python safe-path mode avoids target-package shadowing. The prompt is read from
its file. A fresh prepared conversation has `conversation_id=None`; a prepared
named conversation requires exactly its matching `--resume CONVERSATION_ID`.

The supervisor locates `codex` on PATH. The independently authored external fake
executable implements these public process contracts:

```text
codex app-server --listen unix://ABSOLUTE_SOCKET [-c KEY=TOML_VALUE]...
codex --remote unix://ABSOLUTE_SOCKET -C WORKTREE resume -- EXACT_ROOT
```

The app-server is bidirectional JSON-RPC over Unix WebSocket. Initialization uses
`clientInfo` and `capabilities.experimentalApi=true`, then the `initialized`
notification. Exact `thread/resume` subscribes; `thread/read` alone does not.
Each native connection must support multiple overlapping request IDs and route
each correlated reply without blocking polling or a second active steer behind
another request's delayed ACK. Native mutation order remains serialized, and
per-connection JSON-RPC IDs remain distinct from durable client-message IDs.

Use the full original 0.156.1 generated schemas and OWN provider-draft for native
Thread/Turn/items/pages. Earlier T4 minimum examples and its fixture -32602 error
are not exact-baseline schemas or additional safe-retry evidence. T6's approved
expected-active-turn rejection declaration governs that classification.

Sources: approved design.md “Controlled Codex session”, and existing public T4
wire-and-fixture-contracts.md. Only public executable declarations are carried;
no production or previous fixture bodies are supplied.

## Retained Codex app-server process argument declarations

Approved public setup supplement. Existing process boundary includes retained CLI configuration overrides:

```text
codex app-server --listen unix://ABSOLUTE_SOCKET [-c KEY=TOML_VALUE]...
```

The zero-or-more paired `-c` options configure the existing model, approval_policy, sandbox_mode, trusted-project table, notify hook, and optional model_reasoning_effort settings. The local independently authored codex process adapter must accept these public configuration arguments in addition to the mandatory app-server/listen/socket arguments. They are configuration overrides, not additional provider/model calls. No assertion is made about their order or exact generated serialization. Keep recording their actual argv in the owned capture.

The attached terminal contract remains:

```text
codex --remote unix://ABSOLUTE_SOCKET -C WORKTREE resume -- EXACT_ROOT
```

This document supplies only the existing external command shape, no implementation body or author design. No production changes are needed to accommodate a provider adapter that omitted these retained options.

## Retained active recovery event

`{type:'turn/recovered',thread_id:exact_root,turn_id:nonempty_turn,status:'inProgress'}` is the retained recovery observation. A recovered `status:'completed'` event is unsupported and rejected. A normal completed history/read supplies no main Stop, receipt settlement or current lifecycle rewrite. Exercise read-alone receipt preservation through the actual native thread/read/gateway boundary, rather than inventing a successful completed-recovery mutation.
