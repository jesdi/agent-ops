"""OWN bounded public app-server RPC peer for H03 only; not native evidence.

Records public subscription requests and supplies generic schema-declared native
observations. It never supplies a host runtime view, affirmative claim, ACK or
history receipt, and refuses all model/input/root-creation requests.
"""
import asyncio
import copy
import importlib
import json
from types import SimpleNamespace


def two_public_attachments(case, host, key):
    async def exercise():
        from websockets.asyncio.server import unix_serve
        module = importlib.import_module("dispatcher.codex_supervisor")
        attach = getattr(module, "attach_controller", None)
        case.assertTrue(callable(attach), "Missing behavior: supported controller attachment")
        transport = importlib.import_module("dispatcher.codex_transport")
        binding = copy.deepcopy(host.binding(key))
        root = binding["conversation_id"]
        main_turn = host.view(key)["main"]["turn_id"]
        thread = {"id": root, "preview": "generic OWN fixture", "ephemeral": False,
                  "modelProvider": "fixture", "createdAt": 1, "updatedAt": 2,
                  "status": {"type": "active", "activeFlags": []},
                  "cwd": host.tasks[key].worktree, "cliVersion": "0.156.1",
                  "source": "vscode", "projectId": None, "sessionId": "owned-rpc-session",
                  "turns": [{"id": main_turn, "items": [], "status": "inProgress",
                             "error": None, "itemsView": "full"}]}
        requests, refused, connections = [], [], []

        async def peer(connection):
            connections.append(connection)
            async for raw in connection:
                packet = json.loads(raw)
                method, params = packet.get("method"), packet.get("params", {})
                if "id" not in packet:
                    case.assertEqual(method, "initialized")
                    continue
                requests.append({"method": method, "params": copy.deepcopy(params)})
                if method == "initialize":
                    result = {"userAgent": "OWN H03 declared-RPC fixture", "codexHome": str(host.root / "codex-home"),
                              "platformFamily": "unix", "platformOs": "macos"}
                elif method == "thread/resume":
                    case.assertEqual(params["threadId"], root)
                    case.assertNotIn("input", params)
                    result = {"thread": thread, "model": "generic-fixture", "modelProvider": "fixture",
                              "cwd": host.tasks[key].worktree, "approvalPolicy": "never",
                              "approvalsReviewer": "user", "sandbox": {"type": "readOnly"}}
                elif method == "thread/read":
                    case.assertEqual(params["threadId"], root)
                    result = {"thread": thread}
                elif method == "thread/list":
                    result = {"data": [thread], "nextCursor": None}
                elif method == "thread/turns/list":
                    case.assertEqual(params["threadId"], root)
                    result = {"data": thread["turns"], "nextCursor": None, "backwardsCursor": None}
                elif method in {"thread/items/list", "thread/backgroundTerminals/list"}:
                    case.assertEqual(params["threadId"], root)
                    result = {"data": [], "nextCursor": None, "backwardsCursor": None}
                else:
                    refused.append(method)
                    await connection.send(json.dumps({"id": packet["id"], "error": {
                        "code": -32601, "message": "OWN H03 peer permits subscription/observation only"}}))
                    continue
                await connection.send(json.dumps({"id": packet["id"], "result": result}))

        endpoint = str(host.root / "h03-backend.sock")
        client = host.http.BoundClient(host.state_dir, *key, binding["launch_id"])
        gateway = transport.Gateway(endpoint, root, client, binding)
        arguments = SimpleNamespace(model="generic-fixture", effort="", prompt="MUST NOT SEND THIS ATTACH PROMPT")
        async with unix_serve(peer, path=endpoint):
            for _ in range(2):
                current = copy.deepcopy(client.view()["binding"])
                async with attach(client, current, arguments, backend_path=endpoint,
                                  input_lock=gateway.input_lock) as attachment:
                    ready = await asyncio.wait_for(attachment.wait_ready(), 5)
                    case.assertEqual(ready, current)
                    # Wait for the public native resume to be observed, not a guessed sleep.
                    deadline = asyncio.get_running_loop().time() + 5
                    required = _ + 1
                    while sum(request["method"] == "thread/resume" for request in requests) < required:
                        case.assertLess(asyncio.get_running_loop().time(), deadline)
                        await asyncio.sleep(.005)
                await asyncio.wait_for(attachment.wait_closed(), 5)
            case.assertEqual(sum(request["method"] == "thread/resume" for request in requests), 2)
            case.assertEqual(refused, [], "attachment must not attempt bootstrap/new-root/resend")
        case.assertGreaterEqual(len(connections), 2)
        case.assertEqual(host.binding(key), binding)
        case.assertEqual(host.view(key)["service"], "live")
    asyncio.run(exercise())
