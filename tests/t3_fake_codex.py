"""External Codex process fixture; no dispatcher imports or policy emulation.

Speaks the public JSON-RPC app-server WebSocket protocol. Commands arrive
through a fixture-only file, which is never read by the production supervisor.
"""
import asyncio
import http.client
import json
import os
from pathlib import Path
import sys
import socket as unix_socket

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

ROOT = Path(os.environ["T3_PROVIDER_FIXTURE"])
LOG = ROOT / "provider.jsonl"
THREADS = {}
CLIENTS = {}
NEXT_CONNECTION = 0
NEXT_THREAD = 0
NEXT_TURN = 0


def record(kind, **fields):
    with LOG.open("a") as stream:
        stream.write(json.dumps(dict(kind=kind, pid=os.getpid(), **fields)) + "\n")


def argument(flag):
    for i, value in enumerate(sys.argv):
        if value == flag:
            return sys.argv[i + 1]
        if value.startswith(flag + "="):
            return value.split("=", 1)[1]
    return None


def listener_binding():
    """Read through the public launch-ID-authorized HTTP interface only."""
    connection = http.client.HTTPConnection("localhost")
    connection.sock = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    connection.sock.settimeout(2)
    connection.sock.connect(str(Path(os.environ["AGENT_OPS_STATE_DIR"]) / "wait" / "wait.sock"))
    try:
        payload = {"target": os.environ["AGENT_OPS_TARGET"],
                   "issue": int(os.environ["AGENT_OPS_ISSUE"]),
                   "launch_id": os.environ["AGENT_OPS_LAUNCH_ID"]}
        connection.request("POST", "/runtime/view", json.dumps(payload),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        snapshot = json.loads(response.read())
        return snapshot.get("binding") if isinstance(snapshot, dict) else None
    finally:
        connection.close()


def thread(identity):
    return THREADS.setdefault(identity, {
        "id": identity, "sessionId": identity, "projectId": "fixture-project",
        "preview": "", "ephemeral": False, "modelProvider": "openai",
        "model": "gpt-6.1-sol", "createdAt": 1, "updatedAt": 1,
        "status": {"type": "idle"}, "source": "cli", "cwd": os.getcwd(),
        "cliVersion": "0.156.1", "turns": [],
    })


def thread_response(identity):
    return dict(thread=thread(identity), model="gpt-6.1-sol", modelProvider="openai",
                cwd=os.getcwd(), approvalPolicy="never", approvalsReviewer="user",
                sandbox={"type": "dangerFullAccess"}, reasoningEffort="high")


def listed_threads(params):
    """Native thread/list returns scoped metadata, never hydrated turns."""
    def source_kind(row):
        source = row["source"]
        if isinstance(source, str):
            return source
        subagent = source.get("subAgent")
        if isinstance(subagent, dict) and "thread_spawn" in subagent:
            return "subAgentThreadSpawn"
        return {"review": "subAgentReview", "compact": "subAgentCompact"}.get(
            subagent if isinstance(subagent, str) else None, "subAgentOther")

    def descendant(row, ancestor):
        seen = {row["id"]}
        parent = row.get("parentThreadId")
        while parent and parent not in seen:
            if parent == ancestor:
                return True
            seen.add(parent)
            parent = THREADS.get(parent, {}).get("parentThreadId")
        return False

    sources = params.get("sourceKinds") or ["cli", "vscode", "appServer"]
    rows = []
    for row in THREADS.values():
        if source_kind(row) not in sources:
            continue
        if params.get("ancestorThreadId") and not descendant(row, params["ancestorThreadId"]):
            continue
        if params.get("parentThreadId") and row.get("parentThreadId") != params["parentThreadId"]:
            continue
        rows.append(dict(row, turns=[]))
    return rows


async def broadcast(method, params):
    message = json.dumps(dict(method=method, params=params))
    for client in list(CLIENTS.values()):
        # Codex 0.156.1 does not subscribe a fresh reader to future lifecycle.
        # A new thread or exact thread/resume acquires that root subscription.
        if method.startswith(("turn/", "item/")) and params.get("threadId") not in client["subscriptions"]:
            continue
        try:
            await client["socket"].send(message)
        except Exception:
            pass


async def request(socket):
    global NEXT_CONNECTION, NEXT_THREAD, NEXT_TURN
    NEXT_CONNECTION += 1
    connection = NEXT_CONNECTION
    CLIENTS[connection] = dict(socket=socket, terminal=False, controller=False, subscriptions=set())
    record("connected", connection=connection)
    try:
        async for raw in socket:
            packet = json.loads(raw)
            method = packet.get("method")
            params = packet.get("params") or {}
            record("rpc", connection=connection, method=method, params=params)
            if "id" not in packet:
                continue
            result = None
            error = None
            if method == "initialize":
                CLIENTS[connection]["terminal"] = params.get("clientInfo", {}).get("name") == "t3-terminal"
                result = dict(userAgent="codex/0.156.1", codexHome=os.environ["CODEX_HOME"],
                              platformFamily="unix", platformOs="linux")
            elif method == "thread/start":
                NEXT_THREAD += 1
                identity = f"fixture-main-{NEXT_THREAD}"
                CLIENTS[connection]["subscriptions"].add(identity)
                result = thread_response(identity)
            elif method == "thread/resume":
                CLIENTS[connection]["subscriptions"].add(params["threadId"])
                result = thread_response(params["threadId"])
            elif method == "thread/read":
                result = {"thread": thread(params["threadId"])}
            elif method == "thread/list":
                result = {"data": listed_threads(params), "nextCursor": None}
            elif method == "thread/loaded/list":
                result = {"data": list(THREADS.values()), "nextCursor": None}
            elif method == "thread/turns/list":
                result = {"data": thread(params["threadId"])["turns"], "nextCursor": None}
            elif method == "thread/items/list":
                result = {"data": [{"turnId": turn["id"], "item": item}
                                   for turn in thread(params["threadId"])["turns"]
                                   if not params.get("turnId") or turn["id"] == params["turnId"]
                                   for item in turn["items"]], "nextCursor": None}
            elif method == "thread/backgroundTerminals/list":
                CLIENTS[connection]["controller"] = True
                mode = (ROOT / "mode").read_text()
                if mode == "unsupported":
                    error = {"code": -32601, "message": "required inventory interface unavailable"}
                elif mode == "malformed":
                    result = {"data": "unreadable inventory payload"}
                else:
                    result = {"data": [], "nextCursor": None}
            elif method == "turn/start":
                record("prompt-binding", binding=listener_binding())
                NEXT_TURN += 1
                identity = params["threadId"]
                turn = {"id": f"fixture-turn-{NEXT_TURN}", "status": "inProgress", "items": []}
                thread(identity)["turns"].append(turn)
                thread(identity)["status"] = {"type": "active", "activeFlags": []}
                result = {"turn": turn}
            elif method in {"config/read", "model/list"}:
                result = {"config": {}, "data": [], "nextCursor": None}
            else:
                error = {"code": -32601, "message": f"unsupported fixture method: {method}"}
            reply = dict(id=packet["id"])
            reply["error" if error else "result"] = error or result
            await socket.send(json.dumps(reply))
            if method == "turn/start" and not error:
                await broadcast("turn/started", {"threadId": params["threadId"], "turn": result["turn"]})
    finally:
        CLIENTS.pop(connection, None)
        record("disconnected", connection=connection)


async def commands():
    position = 0
    path = ROOT / "commands.jsonl"
    while True:
        if path.exists():
            with path.open() as stream:
                stream.seek(position)
                lines = stream.readlines()
                position = stream.tell()
            for line in lines:
                command = json.loads(line)
                if command["action"] == "disconnect-control":
                    dropped = []
                    for number, client in list(CLIENTS.items()):
                        if client["controller"] and not client["terminal"]:
                            dropped.append(number)
                            await client["socket"].close(code=1012, reason="fixture control disconnect")
                    record("control-disconnected", connections=dropped)
                elif command["action"] == "output":
                    identity = command["threadId"]
                    turn_id = thread(identity)["turns"][-1]["id"]
                    await broadcast("item/agentMessage/delta", dict(threadId=identity,
                                    turnId=turn_id, itemId="continuation", delta=command["text"]))
                    await broadcast("item/completed", dict(threadId=identity, turnId=turn_id,
                                    item={"type": "agentMessage", "id": "continuation", "text": command["text"]}))
                elif command["action"] == "stop":
                    identity = command["threadId"]
                    turn = thread(identity)["turns"][-1]
                    turn["status"] = "completed"
                    thread(identity)["status"] = {"type": "idle"}
                    await broadcast("turn/completed", {"threadId": identity, "turn": turn})
        await asyncio.sleep(.02)


async def backend():
    listen = argument("--listen")
    if not listen or not listen.startswith("unix://"):
        raise SystemExit("fixture requires the declared Unix WebSocket app-server")
    path = listen[len("unix://"):]
    # A foreign newer root makes implicit newest/last selection observable.
    thread("foreign-newest")
    record("backend-started", argv=sys.argv[1:], listen=listen)
    async with unix_serve(request, path=path):
        await commands()


async def terminal():
    endpoint = argument("--remote")
    if not endpoint or not endpoint.startswith("unix://"):
        raise SystemExit("fixture terminal requires Unix WebSocket remote endpoint")
    identity = None
    if "resume" in sys.argv:
        trailing = sys.argv[sys.argv.index("resume") + 1:]
        if trailing and trailing[0] == "--":
            trailing = trailing[1:]
            identity = trailing[0] if trailing else None
        elif trailing and trailing[0] == "--last":
            identity = "foreign-newest"
        elif trailing and not trailing[0].startswith("-"):
            identity = trailing[0]
    record("terminal-started", argv=sys.argv[1:], endpoint=endpoint, thread_id=identity,
           binding=listener_binding())
    if identity is None:
        raise SystemExit("remote terminal must explicitly attach to the bound root")
    try:
        async with unix_connect(path=endpoint[len("unix://"):]) as socket:
            await socket.send(json.dumps(dict(id="terminal-init", method="initialize",
                              params={"clientInfo": {"name": "t3-terminal", "version": "1"}})))
            await socket.send(json.dumps(dict(method="initialized")))
            await socket.send(json.dumps(dict(id="terminal-root", method="thread/resume",
                                             params={"threadId": identity})))
            async for raw in socket:
                packet = json.loads(raw)
                if packet.get("id") == "terminal-root":
                    observed = packet.get("result", {}).get("thread", {}).get("id")
                    record("terminal-attached", thread_id=observed)
                if packet.get("method") in {"item/agentMessage/delta", "item/completed"}:
                    record("terminal-output", packet=packet)
                    print(json.dumps(packet), flush=True)
    finally:
        if (ROOT / "mode").read_text() == "terminal-survives":
            record("terminal-survived-transport-death")
            await asyncio.Event().wait()


if __name__ == "__main__":
    record("invoked", argv=sys.argv[1:])
    if "--version" in sys.argv or "-V" in sys.argv:
        print("codex 0.156.1")
    elif "app-server" in sys.argv:
        asyncio.run(backend())
    elif "--remote" in sys.argv or any(x.startswith("--remote=") for x in sys.argv):
        asyncio.run(terminal())
    else:
        raise SystemExit("fixture refuses legacy direct/implicit Codex launches")
