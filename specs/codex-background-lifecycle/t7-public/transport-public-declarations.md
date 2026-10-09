# Existing public transport declarations for T7

These declarations describe the unchanged public transport seam available before T7.
They supplement the approved receipt and controller contracts and grant no source-body
access to the isolated acceptance writer.

```python
BoundClient(state_dir, target, issue, launch_id)
BoundClient.view()                              # current exact-launch view
BoundClient.event(binding, event)                # listener acceptance boolean
BoundClient.accept_input(binding, client_message_id)

Gateway(backend_path, conversation, client, binding)
await Gateway.serve(terminal)

async with connect(path, notification) as rpc:
    await rpc.call(method, params, on_result=None)
    request = await rpc.begin(method, params, on_result=None)
    packet = await rpc.finish(request)
```

`BoundClient` is in `dispatcher.runtime_http`. `state_dir` identifies the visible state
layout whose `wait/wait.sock` is the host listener transport; `target` and `launch_id`
are strings and `issue` is an integer. Its view needs no argument. Event and input
calls take the complete declared binding. It has no host prepare/retire credential.
T7 extends `accept_input` only with the separately declared optional `native_input`
keyword; ordinary two-argument calls remain supported.

`Gateway`, `connect` and `RPC` are in `dispatcher.codex_transport`.
`backend_path` is the native Unix WebSocket socket path and `conversation` is the
exact bound root thread ID. The `client` is the existing runtime client and `binding`
is its complete launch binding. A constructed Gateway exposes its real
`input_lock`, an `asyncio.Lock`, shared by terminal admission and attached controller
submission. `serve` takes an accepted terminal WebSocket connection, not a path or
listener address. It serves that connection and ends when its terminal/backend
relay ends; it owns its relay tasks and its per-terminal backend connection.

The public WebSocket server composes `Gateway.serve` as its connection handler,
for example `websockets.asyncio.server.unix_serve(gateway.serve, path=terminal_path)`.
The terminal connects to that real listener. Its owner controls the listener and
terminal lifetime independently of controller context entry/exit. The first-launch
controller context completes before constructing this unchanged Gateway; later
A/B attachments both use this same Gateway's `input_lock` and endpoint.

`connect(path, notification)` is an asynchronous ownership context, not a raw
connection-returning coroutine. `notification` is an awaitable callable accepting
a native JSON-RPC notification packet. Entry performs the native initialize/
initialized exchange and owns a receiver. Context exit closes that controller
connection and awaits its receiver cleanup; it does not close the Gateway or TUI.
`RPC.call` returns the correlated native result and raises `ProtocolError` on a
native error reply. `begin` forwards and returns an opaque request handle;
`finish` awaits that handle and returns the correlated whole response packet.
`on_result`, when supplied, is an awaitable callable invoked for a result before
later notifications on that ordered connection. This is an existing transport
ordering contract, not receipt acceptance or main Stop authority. No controller
approval response or new terminal/transport endpoint is declared.
