"""Independent native 0.156.1 app-server/terminal double, never product state.

Script actions and control messages are fixture-private scheduling instructions.
They aren't Codex RPC fields or RuntimeControl event/mutation APIs.
"""

import asyncio
import copy
import json
import os
from pathlib import Path
import signal
import sys
import uuid

from ws_wire import Closed, connect, serve

VERSION = "0.156.1"
INTERACTIVE = {"cli", "vscode", "exec", "appServer"}
LIST_PARAMS = {
    "thread/list": {"ancestorThreadId", "parentThreadId", "sourceKinds", "cursor",
                    "limit", "sortDirection", "sortKey", "archived", "cwd",
                    "modelProviders", "originators", "projectId", "sectionId",
                    "searchTerm", "useStateDbOnly"},
    "thread/items/list": {"threadId", "turnId", "cursor", "limit", "sortDirection"},
    "thread/backgroundTerminals/list": {"threadId", "cursor", "limit"},
}


def cloned(value):
    return copy.deepcopy(value)


def error(code, message):
    return {"error": {"code": code, "message": message}}


def text_input(text):
    return [{"type": "text", "text": text}]


def user_item(item_id, text, client_id=None):
    return {"type": "userMessage", "id": item_id, "clientId": client_id,
            "content": text_input(text)}


def new_turn(turn_id, *, status="inProgress", items=None, failure=None):
    return {"id": turn_id, "items": cloned(items or []), "itemsView": "full",
            "status": status, "error": cloned(failure)}


def new_thread(thread_id, cwd, *, parent=None, depth=0, source=None):
    if source is None:
        source = "vscode" if parent is None else {
            "subAgent": {"thread_spawn": {"parent_thread_id": parent, "depth": depth,
                                         "agent_path": "/root/" + thread_id}}}
    return {"id": thread_id, "sessionId": thread_id, "parentThreadId": parent,
            "preview": "fixture", "ephemeral": False, "projectId": None,
            "modelProvider": "fixture", "createdAt": 1791301854,
            "updatedAt": 1791301855, "cwd": cwd, "cliVersion": VERSION,
            "source": cloned(source), "status": {"type": "idle"}, "turns": []}


def command_item(item_id, process_id, command, cwd, *, source="unifiedExecStartup"):
    return {"type": "commandExecution", "id": item_id, "command": command,
            "cwd": cwd, "processId": process_id, "source": source,
            "status": "inProgress", "commandActions": [{"type": "unknown", "command": command}],
            "aggregatedOutput": None, "exitCode": None, "durationMs": None}


def source_kind(thread):
    source = thread["source"]
    if isinstance(source, str):
        return source
    sub = source.get("subAgent") if isinstance(source, dict) else None
    if isinstance(sub, dict) and "thread_spawn" in sub:
        return "subAgentThreadSpawn"
    return {"review": "subAgentReview", "compact": "subAgentCompact"}.get(
        sub if isinstance(sub, str) else None, "subAgentOther")


class Journal:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.rows = []
        self.changed = asyncio.Condition()

    async def add(self, kind, **fields):
        row = {"kind": kind, **cloned(fields)}
        self.rows.append(row)
        if self.path:
            with self.path.open("a") as stream:
                stream.write(json.dumps(row, separators=(",", ":")) + "\n")
        async with self.changed:
            self.changed.notify_all()
        return row

    async def wait(self, matches, count=1, timeout=10):
        def found():
            return [r for r in self.rows if all(r.get(k) == v for k, v in matches.items())]
        async def waiting():
            async with self.changed:
                while len(found()) < count:
                    await self.changed.wait()
                return found()
        return await asyncio.wait_for(waiting(), timeout)


class Backend:
    def __init__(self, journal, *, cwd, root_id="root-fixture", page_size=1):
        self.journal = journal
        self.cwd = cwd
        self.root_id = root_id
        self.page_size = page_size
        self.threads = {}
        self.commands = {}
        self.peers = {}
        self.tokens = {}
        self.barriers = []
        self.faults = []
        self.history_views = {}
        self.sequence = 0
        self.stop = asyncio.Event()

    def view_thread(self, thread_id, *, include_turns=False):
        value = cloned(self.threads[thread_id])
        if not include_turns:
            value["turns"] = []
        elif thread_id in self.history_views:
            for turn in value["turns"]:
                turn["items"] = []
                turn["itemsView"] = self.history_views[thread_id]
        return value

    def turn(self, thread_id, turn_id):
        return next(turn for turn in self.threads[thread_id]["turns"] if turn["id"] == turn_id)

    def ancestor(self, thread_id, root):
        seen = set()
        while thread_id in self.threads and thread_id not in seen:
            seen.add(thread_id)
            thread_id = self.threads[thread_id].get("parentThreadId")
            if thread_id == root:
                return True
        return False

    async def notify(self, method, params, *, broadcast=False):
        packet = {"method": method, "params": cloned(params)}
        await self.journal.add("notification", packet=packet)
        thread_id = params.get("threadId")
        for peer_id, peer in list(self.peers.items()):
            if broadcast or any(thread_id == root or self.ancestor(thread_id, root)
                                for root in peer["subscriptions"]):
                try:
                    await peer["wire"].send(packet)
                except (Closed, ConnectionError):
                    await self.journal.add("send_closed", peer=peer_id)

    async def action(self, data):
        """Actions modify independent native state only."""
        name = data["action"]
        if name == "create_thread":
            thread_id = data["thread_id"]
            value = new_thread(thread_id, data.get("cwd", self.cwd),
                               parent=data.get("parent"), depth=data.get("depth", 0),
                               source=data.get("source"))
            value.update(cloned(data.get("overrides", {})))
            self.threads[thread_id] = value
            return self.view_thread(thread_id)
        if name == "start_turn":
            thread_id, turn_id = data["thread_id"], data["turn_id"]
            turn = new_turn(turn_id, items=data.get("items", []))
            self.threads[thread_id]["turns"].append(turn)
            self.threads[thread_id]["status"] = {
                "type": "active", "activeFlags": data.get("active_flags", [])}
            await self.notify("turn/started", {"threadId": thread_id, "turn": turn},
                              broadcast=data.get("broadcast", False))
            return cloned(turn)
        if name == "set_thread":
            self.threads[data["thread_id"]].update(cloned(data["overrides"]))
            return self.view_thread(data["thread_id"], include_turns=True)
        if name == "history_view":
            view = data["items_view"]
            if view not in {"notLoaded", "summary"}:
                raise ValueError("fixture history view must be a declared reduced view")
            self.history_views[data["thread_id"]] = view
            return True
        if name == "end_turn":
            thread_id, turn_id = data["thread_id"], data["turn_id"]
            turn = self.turn(thread_id, turn_id)
            turn["status"] = data.get("status", "completed")
            turn["error"] = cloned(data.get("error"))
            turn["items"].extend(cloned(data.get("items", [])))
            self.threads[thread_id]["status"] = {
                "type": data.get("thread_status", "systemError" if turn["status"] == "failed" else "idle")}
            if turn["status"] == "failed" and turn["error"] is not None:
                await self.notify("error", {"threadId": thread_id, "turnId": turn_id,
                                            "willRetry": False, "error": turn["error"]})
            reduced = cloned(turn)
            reduced["items"] = cloned(data.get("notification_items", []))
            reduced["itemsView"] = "notLoaded" if turn["status"] == "failed" else "summary"
            await self.notify("turn/completed", {"threadId": thread_id, "turn": reduced},
                              broadcast=data.get("broadcast", False))
            return cloned(turn)
        if name == "start_command":
            thread_id, turn_id, item_id = data["thread_id"], data["turn_id"], data["item_id"]
            self.turn(thread_id, turn_id)
            item = command_item(item_id, data.get("process_id", "process-fixture"),
                                data.get("command", "fixture command"), data.get("cwd", self.cwd),
                                source=data.get("source", "unifiedExecStartup"))
            self.commands[(thread_id, item_id)] = {"turn_id": turn_id, "item": item,
                                                   "running": True}
            await self.notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                                               "startedAtMs": 1791302032473, "item": item})
            return cloned(item)
        if name == "end_command":
            thread_id, item_id = data["thread_id"], data["item_id"]
            worker = self.commands[(thread_id, item_id)]
            worker["running"] = False
            item = worker["item"]
            item.update({"status": data.get("status", "completed"),
                         "exitCode": data.get("exit_code"),
                         "aggregatedOutput": data.get("output"),
                         "durationMs": data.get("duration_ms")})
            turn = self.turn(thread_id, worker["turn_id"])
            if not any(i["id"] == item_id for i in turn["items"]):
                turn["items"].append(cloned(item))
            await self.notify("item/completed", {"threadId": thread_id, "turnId": worker["turn_id"],
                                                 "completedAtMs": 1791302039399, "item": item})
            return cloned(item)
        if name == "omit_terminal":
            self.commands[(data["thread_id"], data["item_id"])]["running"] = False
            return True
        if name == "interaction":
            worker = self.commands[(data["thread_id"], data["item_id"])]
            await self.notify("item/commandExecution/terminalInteraction", {
                "threadId": data["thread_id"], "turnId": data["turn_id"],
                "itemId": data["item_id"], "processId": worker["item"]["processId"],
                "stdin": data.get("stdin", "PING")})
            return True
        if name == "output_delta":
            worker = self.commands[(data["thread_id"], data["item_id"])]
            await self.notify("item/commandExecution/outputDelta", {
                "threadId": data["thread_id"], "turnId": worker["turn_id"],
                "itemId": data["item_id"], "delta": data["delta"]})
            return True
        if name == "notification":
            await self.notify(data["method"], data["params"], broadcast=data.get("broadcast", False))
            return True
        if name == "hold_next":
            gate = {"name": data["name"], "matches": data.get("matches", {}),
                    "ready": asyncio.Event(), "claimed": False, "phase": data.get("phase", "after")}
            self.barriers.append(gate)
            return True
        if name == "release":
            gate = next(g for g in self.barriers if g["name"] == data["name"])
            gate["ready"].set()
            return True
        if name == "fault_next":
            self.faults.append({"matches": data.get("matches", {}), "fault": data["fault"],
                                "remaining": data.get("times", 1)})
            return True
        if name == "clear_faults":
            self.faults.clear()
            return True
        if name == "wait":
            return await self.journal.wait(data["matches"], data.get("count", 1), data.get("timeout", 10))
        if name == "disconnect":
            for peer_id, peer in list(self.peers.items()):
                if data.get("peer") in (None, peer_id) and (
                    data.get("client_name") is None or peer["client_name"] == data["client_name"]):
                    await peer["wire"].close()
            return True
        if name == "snapshot":
            return {"threads": cloned(self.threads),
                    "commands": [cloned({"thread_id": k[0], **v}) for k, v in self.commands.items()],
                    "peers": [{"id": k, "client_name": v["client_name"],
                               "subscriptions": sorted(v["subscriptions"])} for k, v in self.peers.items()]}
        if name == "shutdown":
            self.stop.set()
            return True
        raise ValueError("unsupported fixture action: " + name)

    def rows(self, method, params):
        if method == "thread/list":
            kinds = set(params.get("sourceKinds") or INTERACTIVE)
            values = [t for t in self.threads.values() if source_kind(t) in kinds]
            if params.get("ancestorThreadId"):
                values = [t for t in values if self.ancestor(t["id"], params["ancestorThreadId"])]
            if params.get("parentThreadId"):
                values = [t for t in values if t.get("parentThreadId") == params["parentThreadId"]]
            if params.get("archived"):
                values = []
            if params.get("cwd"):
                cwds = params["cwd"] if isinstance(params["cwd"], list) else [params["cwd"]]
                values = [t for t in values if t["cwd"] in cwds]
            if params.get("modelProviders"):
                values = [t for t in values if t["modelProvider"] in params["modelProviders"]]
            if "projectId" in params:
                values = [t for t in values if t["projectId"] == params["projectId"]]
            if params.get("sectionId") is not None:
                values = []
            if params.get("searchTerm"):
                values = [t for t in values if params["searchTerm"] in t["preview"]]
            sort_key = {"created_at": "createdAt", "updated_at": "updatedAt",
                        "recency_at": "recencyAt", "section_position": "sectionPosition"}.get(
                            params.get("sortKey"), "createdAt")
            values = sorted(values, key=lambda t: (t.get(sort_key, 0), t["id"]))
            return [self.view_thread(t["id"]) for t in values]
        thread_id = params["threadId"]
        if method == "thread/backgroundTerminals/list":
            if self.threads[thread_id]["status"]["type"] == "notLoaded":
                raise KeyError("thread not found")
            return [{"itemId": w["item"]["id"], "processId": w["item"]["processId"],
                     "command": w["item"]["command"], "cwd": w["item"]["cwd"],
                     "osPid": None, "cpuPercent": None, "rssKb": None}
                    for (owner, _), w in self.commands.items() if owner == thread_id and w["running"]]
        return [{"turnId": t["id"], "item": cloned(item)} for t in self.threads[thread_id]["turns"]
                if not params.get("turnId") or t["id"] == params["turnId"] for item in t["items"]]

    def turns_page(self, params):
        allowed = {"threadId", "cursor", "limit", "sortDirection", "itemsView"}
        if set(params) - allowed or not isinstance(params.get("threadId"), str):
            raise ValueError("undeclared turn-list fields or missing thread scope")
        thread_id = params["threadId"]
        values = cloned(self.threads[thread_id]["turns"])
        direction = params.get("sortDirection")
        direction = "desc" if direction is None else direction
        items_view = params.get("itemsView")
        items_view = "summary" if items_view is None else items_view
        if not isinstance(direction, str) or direction not in {"asc", "desc"}:
            raise ValueError("undeclared turn direction")
        if not isinstance(items_view, str) or items_view not in {"notLoaded", "summary", "full"}:
            raise ValueError("undeclared turn direction or items view")
        limit = params.get("limit")
        limit = self.page_size if limit is None else limit
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 4294967295:
            raise ValueError("turn page limit must be uint32")
        # Schema permits zero; this own server normalizes it to a bounded
        # positive page. No unexercised native zero-limit semantics are claimed.
        limit = min(max(1, limit), max(1, self.page_size))
        scope = ("thread/turns/list", thread_id)
        cursor = params.get("cursor")
        if cursor is None:
            index = len(values) - 1 if direction == "desc" else 0
        else:
            if not isinstance(cursor, str) or cursor not in self.tokens:
                raise ValueError("unknown opaque turn cursor")
            stored = self.tokens[cursor]
            if stored["scope"] != scope:
                raise ValueError("turn cursor method or thread scope mismatch")
            opposite = stored["direction"] != direction
            if opposite != (stored["kind"] == "backwards"):
                raise ValueError("turn cursor direction mismatch")
            anchor = next((i for i, turn in enumerate(values) if turn["id"] == stored["turn_id"]), None)
            if anchor is None:
                raise ValueError("turn cursor anchor unavailable")
            index = anchor if opposite else anchor + (-1 if direction == "desc" else 1)
        step = -1 if direction == "desc" else 1
        indices = list(range(index, -1 if step < 0 else len(values), step))[:limit]
        data = [values[i] for i in indices]
        for turn in data:
            turn["itemsView"] = items_view
            if items_view == "notLoaded":
                turn["items"] = []
        def token(anchor, kind):
            value = "opaque-turn-" + uuid.uuid4().hex
            self.tokens[value] = {"scope": scope, "direction": direction,
                                  "turn_id": values[anchor]["id"], "kind": kind}
            return value
        more = bool(indices) and 0 <= indices[-1] + step < len(values)
        return {"data": data, "nextCursor": token(indices[-1], "next") if more else None,
                "backwardsCursor": token(indices[0], "backwards") if indices else None}

    def page(self, method, params):
        if set(params) - LIST_PARAMS[method]:
            raise ValueError("undeclared list request fields")
        if params.get("ancestorThreadId") and params.get("parentThreadId"):
            raise ValueError("ancestry filters are mutually exclusive")
        if params.get("originators"):
            raise ValueError("local app-server rejects originators filter")
        scope = (method, json.dumps({k: v for k, v in params.items()
                                    if k not in {"cursor", "limit", "sortDirection"}}, sort_keys=True))
        direction = params.get("sortDirection") or ("desc" if method == "thread/list" else "asc")
        limit = params.get("limit")
        limit = self.page_size if limit is None else limit
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("fixture positive page limit required")
        limit = min(limit, self.page_size)
        cursor = params.get("cursor")
        if cursor is None:
            values = cloned(self.rows(method, params))
            index = len(values) - 1 if direction == "desc" else 0
        else:
            stored = self.tokens[cursor]
            if stored["scope"] != scope:
                raise ValueError("cursor scope mismatch")
            values = cloned(stored["rows"])
            anchor = stored["anchor"]
            if method == "thread/items/list":
                # The exercised native checkpoint follows an existing userMessage;
                # later original-command outcomes are appended under that turn.
                # Match the stable public row, without decoding the opaque cursor.
                values = cloned(self.rows(method, params))
                previous = stored["rows"][anchor]
                anchor = next(i for i, row in enumerate(values)
                              if (row["turnId"], row["item"]["id"]) ==
                                 (previous["turnId"], previous["item"]["id"]))
            if stored["direction"] == direction:
                index = anchor + (-1 if direction == "desc" else 1) if stored["kind"] == "next" else anchor
            else:
                index = anchor
        step = -1 if direction == "desc" else 1
        indices = list(range(index, -1 if step < 0 else len(values), step))[:limit]
        data = [values[i] for i in indices]
        next_index = (indices[-1] + step) if indices else index
        more = bool(indices) and 0 <= next_index < len(values)
        def token(next_pos, anchor, kind):
            value = "opaque-" + uuid.uuid4().hex
            self.tokens[value] = {"scope": scope, "rows": values, "direction": direction,
                                  "index": next_pos, "anchor": anchor, "kind": kind}
            return value
        result = {"data": data, "nextCursor": token(next_index, indices[-1], "next") if more else None}
        if method != "thread/backgroundTerminals/list":
            result["backwardsCursor"] = token(indices[0], indices[0], "backwards") if indices else None
        return result

    async def rpc_result(self, peer_id, method, params):
        if method == "initialize":
            self.peers[peer_id]["client_name"] = params["clientInfo"]["name"]
            return {"userAgent": "codex-cli/" + VERSION, "codexHome": os.environ["CODEX_HOME"],
                    "platformFamily": "unix", "platformOs": "linux"}
        if method in ("thread/start", "thread/resume"):
            thread_id = params.get("threadId", self.root_id)
            if method == "thread/start":
                if thread_id in self.threads:
                    raise ValueError("fixture root already exists")
                self.threads[thread_id] = new_thread(thread_id, params.get("cwd", self.cwd))
            elif thread_id not in self.threads:
                raise KeyError("thread not found")
            self.peers[peer_id]["subscriptions"].add(thread_id)
            if self.threads[thread_id]["status"]["type"] == "notLoaded":
                self.threads[thread_id]["status"] = {"type": "idle"}
            return {"thread": self.view_thread(thread_id, include_turns=True),
                    "model": "fixture", "modelProvider": "fixture", "cwd": self.cwd,
                    "approvalPolicy": "never", "approvalsReviewer": "user",
                    "sandbox": {"type": "dangerFullAccess"}}
        if method == "thread/read":
            return {"thread": self.view_thread(params["threadId"], include_turns=params.get("includeTurns", False))}
        if method == "thread/turns/list":
            return self.turns_page(params)
        if method in LIST_PARAMS:
            return self.page(method, params)
        if method == "turn/start":
            thread_id = params["threadId"]
            if self.threads[thread_id]["status"]["type"] == "active":
                raise ValueError("thread already has an active turn")
            self.sequence += 1
            turn_id = "turn-input-" + str(self.sequence)
            item = {"type": "userMessage", "id": "input-item-" + str(self.sequence),
                    "clientId": params.get("clientUserMessageId"), "content": cloned(params["input"])}
            return {"turn": await self.action({"action": "start_turn", "thread_id": thread_id,
                                               "turn_id": turn_id, "items": [item]})}
        if method == "turn/steer":
            thread = self.threads[params["threadId"]]
            active = thread["turns"][-1] if thread["turns"] else None
            if not active or active["status"] != "inProgress" or active["id"] != params["expectedTurnId"]:
                raise ValueError("expected active turn did not match")
            self.sequence += 1
            active["items"].append({"type": "userMessage", "id": "input-item-" + str(self.sequence),
                                    "clientId": params.get("clientUserMessageId"),
                                    "content": cloned(params["input"])})
            return {"turnId": active["id"]}
        raise NotImplementedError(method)

    async def respond(self, peer_id, packet):
        method, params = packet["method"], packet.get("params", {})
        peer = self.peers[peer_id]
        await self.journal.add("rpc_received", peer=peer_id, method=method, params=params, request_id=packet["id"])
        flat = {"method": method, **params}
        async def hold(phase):
            for gate in self.barriers:
                if gate["phase"] == phase and not gate["claimed"] and all(
                    flat.get(k) == v for k, v in gate["matches"].items()):
                    gate["claimed"] = True
                    await self.journal.add("request_held", name=gate["name"], peer=peer_id,
                                           method=method, params=params)
                    await gate["ready"].wait()
                    break
        await hold("before")
        try:
            result = await self.rpc_result(peer_id, method, params)
            reply = {"id": packet["id"], "result": result}
        except NotImplementedError:
            reply = {"id": packet["id"], **error(-32601, "fixture method unsupported")}
        except (KeyError, ValueError) as exc:
            reply = {"id": packet["id"], **error(-32602, str(exc))}
        for fault in list(self.faults):
            if all(flat.get(k) == v for k, v in fault["matches"].items()):
                fault["remaining"] -= 1
                if fault["remaining"] <= 0:
                    self.faults.remove(fault)
                if fault["fault"] == "missing_cursor":
                    reply.get("result", {}).pop("nextCursor", None)
                elif fault["fault"] == "repeat_cursor":
                    page = reply.get("result", {})
                    if page.get("nextCursor") is not None:
                        fault.setdefault("repeated", page["nextCursor"])
                        page["nextCursor"] = fault["repeated"]
                    elif params.get("cursor") is not None:
                        page["nextCursor"] = params["cursor"]
                elif fault["fault"] == "error":
                    reply = {"id": packet["id"], **error(-32603, "fixture query unreadable")}
                elif isinstance(fault["fault"], dict):
                    reply = {"id": packet["id"], **cloned(fault["fault"])}
                else:
                    raise ValueError("unsupported fixture fault")
                break
        await hold("after")
        try:
            await peer["wire"].send(reply)
            await self.journal.add("rpc_response", peer=peer_id, method=method, packet=reply)
        except (Closed, ConnectionError):
            await self.journal.add("response_closed", peer=peer_id, method=method)

    async def connection(self, wire):
        peer_id = "peer-" + uuid.uuid4().hex
        self.peers[peer_id] = {"wire": wire, "subscriptions": set(), "client_name": None}
        tasks = set()
        await self.journal.add("connected", peer=peer_id)
        try:
            while True:
                packet = await wire.receive()
                if "method" in packet and "id" in packet:
                    task = asyncio.create_task(self.respond(peer_id, packet))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                else:
                    await self.journal.add("client_notification", peer=peer_id, packet=packet)
        except Closed:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.peers.pop(peer_id, None)
            await self.journal.add("disconnected", peer=peer_id)
            await wire.close()


async def action_server(path, handler):
    async def connected(reader, writer):
        try:
            data = json.loads(await reader.readline())
            result = await handler(data)
            writer.write(json.dumps({"ok": True, "result": result}).encode() + b"\n")
        except Exception as exc:
            writer.write(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")
        finally:
            try:
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()
            await writer.wait_closed()
    if Path(path).exists():
        raise ValueError("fixture refuses to replace an existing control socket")
    return await asyncio.start_unix_server(connected, path)


async def run_backend(socket_path):
    journal = Journal(os.environ.get("FAKE_CODEX_JOURNAL"))
    backend = Backend(journal, cwd=os.environ.get("FAKE_CODEX_CWD", os.getcwd()),
                      root_id=os.environ.get("FAKE_CODEX_ROOT", "root-fixture"),
                      page_size=int(os.environ.get("FAKE_CODEX_PAGE_SIZE", "1")))
    if os.environ.get("FAKE_CODEX_SCRIPT"):
        for action in json.loads(Path(os.environ["FAKE_CODEX_SCRIPT"]).read_text()):
            await backend.action(action)
    control_path = os.environ["FAKE_CODEX_CONTROL"]
    if Path(socket_path).exists():
        raise ValueError("fixture refuses to replace an existing app-server socket")
    server = await serve(backend.connection, socket_path)
    control_server = await action_server(control_path, backend.action)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, backend.stop.set)
    await journal.add("ready", socket=socket_path, control=control_path)
    try:
        await backend.stop.wait()
    finally:
        server.close()
        control_server.close()
        await server.wait_closed()
        await control_server.wait_closed()
        for peer in list(backend.peers.values()):
            await peer["wire"].close()
        for path in (socket_path, control_path):
            Path(path).unlink(missing_ok=True)
        await journal.add("stopped")


async def run_terminal(socket_path, root):
    journal = Journal(os.environ.get("FAKE_TERMINAL_JOURNAL"))
    wire = await connect(socket_path)
    pending = {}
    sequence = 0
    stopped = asyncio.Event()
    async def reader():
        try:
            while True:
                packet = await wire.receive()
                await journal.add("terminal_packet", packet=packet)
                if "id" in packet and "method" not in packet:
                    waiter = pending.pop(packet["id"], None)
                    if waiter:
                        waiter.set_result(packet)
                elif "id" in packet:
                    await wire.send({"id": packet["id"], "result": None})
        except Closed:
            stopped.set()
    async def call(method, params):
        nonlocal sequence
        sequence += 1
        request_id = "terminal-" + str(sequence)
        waiter = asyncio.get_running_loop().create_future()
        pending[request_id] = waiter
        await wire.send({"id": request_id, "method": method, "params": params})
        return await asyncio.wait_for(waiter, 10)
    receive_task = asyncio.create_task(reader())
    await call("initialize", {"clientInfo": {"name": "fixture-terminal", "version": VERSION},
                              "capabilities": {"experimentalApi": True}})
    await wire.send({"method": "initialized", "params": {}})
    response = await call("thread/resume", {"threadId": root})
    if "error" in response:
        raise ValueError("fixture terminal root resume rejected")
    await journal.add("terminal_attached", root=root, pid=os.getpid())
    async def action(data):
        if data["action"] == "rpc":
            return await call(data["method"], data.get("params", {}))
        if data["action"] == "snapshot":
            return {"root": root, "pid": os.getpid(), "rows": cloned(journal.rows)}
        if data["action"] == "wait":
            return await journal.wait(data["matches"], data.get("count", 1), data.get("timeout", 10))
        if data["action"] == "shutdown":
            stopped.set()
            return True
        raise ValueError("unsupported terminal fixture action")
    control_path = os.environ["FAKE_TERMINAL_CONTROL"]
    server = await action_server(control_path, action)
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    try:
        await stopped.wait()
    finally:
        server.close()
        await server.wait_closed()
        await wire.close()
        receive_task.cancel()
        await asyncio.gather(receive_task, return_exceptions=True)
        Path(control_path).unlink(missing_ok=True)
        await journal.add("terminal_stopped")


def socket_uri(value):
    if not value.startswith("unix:///"):
        raise ValueError("fixture requires an absolute unix:// socket URI")
    return value[len("unix://"):]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print("codex-cli " + VERSION)
        return
    if args[:1] == ["app-server"] and "--listen" in args:
        asyncio.run(run_backend(socket_uri(args[args.index("--listen") + 1])))
        return
    if "--remote" in args and "resume" in args and "--" in args:
        asyncio.run(run_terminal(socket_uri(args[args.index("--remote") + 1]), args[args.index("--") + 1]))
        return
    raise SystemExit("unsupported independent fixture codex invocation")


if __name__ == "__main__":
    main()
