"""Owned external Codex protocol fixture; no production control logic lives here."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys

import websockets


ROOT = Path(os.environ["T4_EXTERNAL"])
THREAD = "root"


def log(kind, **fields):
    with (ROOT / "wire.jsonl").open("a") as stream:
        stream.write(json.dumps({"kind": kind, **fields}) + "\n")


def control():
    return json.loads((ROOT / "control.json").read_text())


def commands(name):
    path = ROOT / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def backend(path):
    peers = set()
    thread = {"id": THREAD, "status": {"type": "idle"}, "turns": []}
    next_turn = 0

    async def send(peer, packet):
        try:
            await peer.send(json.dumps(packet))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def handle_packet(peer, packet):
        nonlocal next_turn
        log("backend_receive", packet=packet)
        if "method" not in packet:
            return
        method, params = packet["method"], packet.get("params", {})
        if "id" not in packet:
            return
        ident = packet["id"]
        if method == "initialize":
            result = {"userAgent": "isolated T4 protocol fixture"}
        elif method == "thread/start":
            result = {"thread": copy.deepcopy(thread)}
        elif method in ("thread/read", "thread/resume"):
            while control().get("hold_reads"):
                await asyncio.sleep(0.01)
            result = {"thread": copy.deepcopy(thread)}
        elif method == "thread/backgroundTerminals/list":
            result = {"data": [], "nextCursor": None}
        elif method in ("turn/start", "turn/steer"):
            client_id = params.get("clientUserMessageId", "")
            behavior = control().get("inputs", {}).get(client_id, "accept")
            log("input_received", packet=packet, client_id=client_id)
            while behavior == "defer":
                await asyncio.sleep(0.01)
                behavior = control().get("inputs", {}).get(client_id, "accept")
            if behavior == "reject":
                await send(peer, {"id": ident, "error": {"code": -32602,
                           "message": "expected active turn did not match"}})
                return
            if method == "turn/start":
                next_turn += 1
                turn = {"id": f"turn-{next_turn}", "status": "inProgress", "items": [
                    {"type": "userMessage", "id": f"item-{next_turn}",
                     "clientId": client_id, "content": params.get("input", [])}]}
                thread["turns"].append(turn)
            else:
                turn = thread["turns"][-1]
                turn["items"].append({"type": "userMessage", "id": f"steer-{ident}",
                                      "clientId": client_id, "content": params.get("input", [])})
            thread["status"] = {"type": "active"}
            if behavior == "lose":
                await peer.close()
                return
            if behavior == "hold":
                while control().get("inputs", {}).get(client_id) == "hold":
                    await asyncio.sleep(0.01)
            result = ({"turn": copy.deepcopy(turn)} if method == "turn/start"
                      else {"turnId": turn["id"]})
            await send(peer, {"id": ident, "result": result})
            if behavior == "accept":
                notice = {"method": "turn/started", "params": {"threadId": THREAD,
                          "turn": copy.deepcopy(turn)}}
                for connection in tuple(peers):
                    await send(connection, notice)
            return
        else:
            result = {"echo": params}
        await send(peer, {"id": ident, "result": result})

    async def handler(peer):
        peers.add(peer)
        tasks = []
        try:
            async for raw in peer:
                task = asyncio.create_task(handle_packet(peer, json.loads(raw)))
                tasks.append(task)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            peers.discard(peer)
            for task in tasks:
                task.cancel()

    async def pump():
        cursor = 0
        while True:
            pending = commands("backend.commands")
            for command in pending[cursor:]:
                if command["action"] == "complete":
                    if thread["turns"]:
                        thread["turns"][-1]["status"] = "completed"
                        thread["status"] = {"type": "idle"}
                        packet = {"method": "turn/completed", "params": {"threadId": THREAD,
                                  "turn": copy.deepcopy(thread["turns"][-1])}}
                        for peer in tuple(peers):
                            await send(peer, packet)
                elif command["action"] == "broadcast":
                    for peer in tuple(peers):
                        await send(peer, command["packet"])
                log("backend_command", token=command["token"])
            cursor = len(pending)
            await asyncio.sleep(0.01)

    async with websockets.unix_serve(handler, path=path):
        log("backend_ready", path=path)
        await pump()


async def terminal(path):
    peers = {}
    readers = []

    async def reader(name, peer):
        try:
            async for raw in peer:
                log("terminal_receive", connection=name, packet=json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            log("terminal_closed", connection=name)

    async def connect(name):
        peer = await websockets.unix_connect(path)
        peers[name] = peer
        readers.append(asyncio.create_task(reader(name, peer)))
        log("terminal_ready", connection=name, argv=sys.argv[1:])

    await connect("primary")
    cursor = 0
    try:
        while True:
            pending = commands("terminal.commands")
            for command in pending[cursor:]:
                name = command.get("connection", "primary")
                if command["action"] == "connect":
                    await connect(name)
                else:
                    try:
                        await peers[name].send(json.dumps(command["packet"]))
                        log("terminal_send", connection=name, packet=command["packet"])
                    except websockets.exceptions.ConnectionClosed:
                        log("terminal_closed", connection=name)
                log("terminal_command", token=command["token"])
            cursor = len(pending)
            await asyncio.sleep(0.01)
    finally:
        for peer in peers.values():
            await peer.close()
        for task in readers:
            task.cancel()


def main():
    log("fixture_process", pid=os.getpid(), pgid=os.getpgrp())
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "app-server":
        address = arguments[arguments.index("--listen") + 1]
        asyncio.run(backend(address.removeprefix("unix://")))
    elif "--remote" in arguments:
        address = arguments[arguments.index("--remote") + 1]
        asyncio.run(terminal(address.removeprefix("unix://")))
    else:
        raise SystemExit(f"unexpected fixture invocation: {arguments}")


if __name__ == "__main__":
    main()
