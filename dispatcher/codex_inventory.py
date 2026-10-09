"""Complete native owned-work scans; provider reads run outside RPC callbacks."""
from copy import deepcopy

from dispatcher.codex_transport import ProtocolError
from dispatcher.runtime_work import nonempty, valid_ancestry, identity_key


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


async def pages(rpc, method, params, validate):
    """A cursor belongs only to this method/scope and must reach explicit null."""
    rows, seen, cursor = [], set(), None
    while True:
        result = await rpc.call(method, {**params, "cursor": cursor})
        require(isinstance(result, dict), f"Unreadable {method} result")
        require(isinstance(result.get("data"), list) and "nextCursor" in result, f"Incomplete {method} page")
        require(all(validate(row) for row in result["data"]), f"Unreadable {method} row")
        rows.extend(result["data"])
        cursor = result["nextCursor"]
        if cursor is None:
            return rows
        require(nonempty(cursor) and cursor not in seen, f"Invalid or repeated {method} cursor")
        seen.add(cursor)


def native_status(status):
    if not isinstance(status, dict) or status.get("type") not in ("active", "idle", "systemError", "notLoaded"):
        return False
    if status["type"] != "active":
        return True
    flags = status.get("activeFlags")
    return isinstance(flags, list) and all(flag in ("waitingOnApproval", "waitingOnUserInput") for flag in flags)


def native_thread(thread):
    return (isinstance(thread, dict) and nonempty(thread.get("id"))
            and native_status(thread.get("status")) and isinstance(thread.get("turns"), list))


def native_turn(turn):
    return (isinstance(turn, dict) and nonempty(turn.get("id"))
            and turn.get("status") in ("inProgress", "completed", "failed", "interrupted")
            and isinstance(turn.get("items"), list))


def native_item(entry):
    if not isinstance(entry, dict) or not nonempty(entry.get("turnId")):
        return False
    item = entry.get("item")
    return isinstance(item, dict) and nonempty(item.get("id")) and nonempty(item.get("type"))


def native_terminal(row):
    return isinstance(row, dict) and all(nonempty(row.get(k)) for k in ("itemId", "processId", "command", "cwd"))


def ownership_node(thread, root):
    identity = thread["id"]
    if identity == root:
        return dict(thread_id=root, parent_thread_id=None, source_kind="bound-root", source_parent_thread_id=None, depth=0)
    source = thread.get("source")
    sub = source.get("subAgent") if isinstance(source, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    require(isinstance(spawn, dict), "Descendant lacks native spawned ownership")
    return dict(thread_id=identity, parent_thread_id=thread.get("parentThreadId"), source_kind="thread-spawn",
                source_parent_thread_id=spawn.get("parent_thread_id"), depth=spawn.get("depth"))


def ancestry_for(owner, nodes, root):
    result, seen, current = [], set(), owner
    while current is not None:
        require(current in nodes and current not in seen, "Incomplete or cyclic native ancestry")
        seen.add(current)
        node = nodes[current]
        result.append(node)
        current = node["parent_thread_id"]
    require(valid_ancestry(result, owner, root), "Contradictory native ancestry")
    return result


def command_observation(owner, turn, item, ancestry):
    status = {"inProgress": "running", "completed": "completed", "failed": "failed", "declined": "declined"}.get(item.get("status"))
    require(status is not None, "Unreadable command status")
    outcome = None
    if status != "running":
        outcome = dict(status=status, exit_code=item.get("exitCode"), aggregated_output=item.get("aggregatedOutput"), duration_ms=item.get("durationMs"))
    return dict(identity=dict(kind="command", thread_id=owner, initial_item_id=item["id"]),
                turn_id=turn, ancestry=ancestry, process_id=item.get("processId"), command=item.get("command"),
                cwd=item.get("cwd"), source=item.get("source"), status=status, inventory_running=False,
                normal_stop=None, outcome=outcome)


def agent_outcome(turn, items):
    if turn["status"] == "inProgress":
        return None
    messages = [dict(item_id=item["id"], text=item["text"]) for item in items if item["type"] == "agentMessage"]
    error = turn.get("error")
    if error is not None:
        require(isinstance(error, dict) and isinstance(error.get("message"), str), "Unreadable child error")
        error = dict(message=error["message"], codex_error_info=error.get("codexErrorInfo"))
    return dict(status=turn["status"], messages=messages, error=error)


def agent_observation(thread, turn, items, ancestry):
    status = thread["status"]
    require(native_status(status), "Unreadable child thread status")
    outcome = agent_outcome(turn, items)
    return dict(identity=dict(kind="agent", thread_id=thread["id"], turn_id=turn["id"]), ancestry=ancestry,
                thread_status=status["type"], active_flags=status.get("activeFlags", []),
                status="running" if outcome is None else outcome["status"], outcome=outcome)


class NativeInventory:
    """Retain initial item observations missing from native running history.

    The listener remains the durable authority. This connection-independent cache
    only supplies native initial item/turn evidence and is never an empty-work proof.
    """
    def __init__(self):
        self.items = {}
        self.ends = {}
        self.outcomes = {}
        self.messages = {}
        self.thread_statuses = {}
        self.nodes = {}
        self.activity = 0

    def remember_item(self, owner, turn, item):
        if item.get("type") != "commandExecution" or item.get("source") == "unifiedExecInteraction":
            return
        require(nonempty(owner) and nonempty(turn) and nonempty(item.get("id")), "Unreadable command identity")
        key = (owner, item["id"])
        previous = self.items.get(key)
        if previous:
            require(turn == previous[0], "Contradictory initial command owning turn")
            if previous[1].get("status") != "inProgress":
                return
        self.items[key] = (turn, deepcopy(item))

    def remember_end(self, owner, turn, source):
        if turn.get("status") == "failed" and isinstance(turn.get("error"), dict):
            self.outcomes[(owner, turn["id"])] = deepcopy(turn)
        if turn.get("status") == "completed":
            self.ends[(owner, turn["id"])] = dict(thread_id=owner, turn_id=turn["id"], status="completed", evidence_source=source)

    def notification(self, packet, root):
        method, params = packet.get("method"), packet.get("params")
        if not isinstance(params, dict):
            return False
        owner = params.get("threadId")
        if method in ("item/started", "item/completed"):
            item = params.get("item")
            require(isinstance(item, dict), "Unreadable native item notification")
            self.remember_item(owner, params.get("turnId"), item)
            relevant = item.get("type") in ("commandExecution", "subAgentActivity")
        elif method in ("turn/started", "turn/completed") and owner in self.nodes and owner != root:
            turn = params.get("turn")
            require(native_turn(turn), "Unreadable child turn notification")
            self.remember_end(owner, turn, "native-child-event")
            relevant = True
        else:
            return False
        self.activity += 1
        return relevant and (owner == root or owner in self.nodes)

    async def discover(self, rpc, root):
        rows = await pages(rpc, "thread/list", dict(ancestorThreadId=root,
            sourceKinds=["subAgentThreadSpawn"], modelProviders=[]), native_thread)
        nodes = {root: ownership_node({"id": root}, root)}
        statuses = {}
        for row in rows:
            nodes[row["id"]] = ownership_node(row, root)
            statuses[row["id"]] = deepcopy(row["status"])
        # Previously discovered owners cannot silently disappear from discovery.
        for owner in self.nodes.keys() - nodes.keys():
            response = await rpc.call("thread/read", dict(threadId=owner, includeTurns=False))
            require(isinstance(response, dict), "Unreadable retained owner response")
            thread = response.get("thread")
            require(native_thread(thread) and thread["id"] == owner, "Unreadable retained owner")
            nodes[owner] = ownership_node(thread, root)
            statuses[owner] = deepcopy(thread["status"])
        ancestry = {owner: ancestry_for(owner, nodes, root) for owner in nodes}
        # Publish observed metadata with ownership even if the first full read fails.
        self.thread_statuses.update(statuses)
        self.nodes = nodes
        return ancestry

    async def read_thread(self, rpc, owner):
        response = await rpc.call("thread/read", dict(threadId=owner, includeTurns=True))
        require(isinstance(response, dict), "Unreadable exact-thread history response")
        thread = response.get("thread")
        require(native_thread(thread) and thread["id"] == owner, "Unreadable exact-thread history")
        return thread

    async def loaded_history(self, rpc, owner):
        thread = await self.read_thread(rpc, owner)
        if thread["status"].get("type") == "notLoaded":
            response = await rpc.call("thread/resume", dict(threadId=owner))
            require(isinstance(response, dict), "Unreadable exact-owner load response")
            require(native_thread(response.get("thread")) and response["thread"]["id"] == owner, "Unreadable exact-owner load")
            # Resume can return paginated/reduced turns. Read full exact history.
            thread = await self.read_thread(rpc, owner)
        require(all(native_turn(t) for t in thread["turns"]), "Unreadable native turns")
        return thread

    def remember_turns(self, owner, thread):
        self.thread_statuses[owner] = deepcopy(thread["status"])
        for turn in thread["turns"]:
            self.remember_end(owner, turn, "authoritative-child-history")

    def remember_entries(self, owner, thread, entries):
        for entry in entries:
            # Full scoped history can discover a startup missed before this
            # observer subscribed. remember_item retains its initial identity;
            # only the separate owning-Stop rules can qualify it as work.
            self.remember_item(owner, entry["turnId"], entry["item"])
        for turn in thread["turns"]:
            items = turn_items(entries, turn["id"])
            self.messages[(owner, turn["id"])] = items
            if turn["status"] != "inProgress":
                self.outcomes[(owner, turn["id"])] = deepcopy(turn)
            for item in items:
                self.remember_item(owner, turn["id"], item)

    async def history(self, rpc, owner):
        thread = await self.loaded_history(rpc, owner)
        # Definite errors remain usable even if the owner stays unloaded.
        self.remember_turns(owner, thread)
        item_error = None
        entries = []
        try:
            entries = await pages(rpc, "thread/items/list", dict(threadId=owner, sortDirection="asc"), native_item)
        except (ProtocolError, KeyError, TypeError, ValueError) as exc:
            item_error = exc
        else:
            entries = merge_history_entries(entries, thread["turns"], messages_only=True)
            self.remember_entries(owner, thread, entries)
        # includeTurns may return a reduced history even when its last visible
        # turn has the current native status. Only exhausted turn pages establish
        # every owning turn, including no-item outcomes and the launch baseline.
        thread["turns"] = await pages(rpc, "thread/turns/list",
            dict(threadId=owner, sortDirection="asc", itemsView="full"), native_turn)
        self.remember_turns(owner, thread)
        entries = merge_history_entries(entries, thread["turns"])
        self.remember_entries(owner, thread, entries)
        if item_error is not None:
            raise item_error
        # Loading failure does not erase readable older outcomes and messages.
        require(thread["status"]["type"] != "notLoaded", "Exact owner history remains notLoaded")
        require(not incomplete_turn_history(thread, entries), "Unreadable current turn or item owning turn")
        return thread, entries

    def commands(self, owner, ancestry, terminals, stops, stored):
        values = stored_commands(stored, owner)
        for (thread, item_id), (turn, item) in self.items.items():
            if thread == owner:
                values[item_id] = command_observation(owner, turn, item, ancestry)
        for item_id, worker in values.items():
            worker["inventory_running"] = item_id in terminals
            worker["normal_stop"] = stops.get((owner, worker["turn_id"]))
            if worker["outcome"] is None and item_id not in terminals:
                worker["status"] = "unknown"
        return list(values.values())

    def partial(self, snapshot):
        root = snapshot["binding"]["conversation_id"]
        workers = []
        for owner in self.nodes:
            ancestry = ancestry_for(owner, self.nodes, root)
            workers.extend(self.commands(owner, ancestry, set(), {}, snapshot["workers"]))
            if owner != root:
                workers.extend(self.ended_agents(owner, ancestry))
        return workers

    def ended_agents(self, owner, ancestry):
        thread = dict(id=owner, status=self.thread_statuses[owner])
        return [agent_observation(thread, turn, self.messages.get((owner, turn["id"]), []), ancestry)
                for (thread_id, _), turn in self.outcomes.items() if thread_id == owner]

    def owning_stops(self, snapshot):
        root = snapshot["binding"]["conversation_id"]
        stops = {key: deepcopy(stop) for key, stop in self.ends.items() if key[0] != root}
        for turn, revision in snapshot["main"]["completed_turns"].items():
            stops[(root, turn)] = dict(thread_id=root, turn_id=turn, status="completed", revision=revision)
        return stops

    async def owner_inventory(self, rpc, snapshot, owner, chain):
        thread, entries = await self.history(rpc, owner)
        # Freeze child-end evidence before requesting any terminal page.
        stops = self.owning_stops(snapshot)
        terminals = await pages(rpc, "thread/backgroundTerminals/list", dict(threadId=owner), native_terminal)
        terminal_ids = {row["itemId"] for row in terminals}
        commands = self.commands(owner, chain, terminal_ids, stops, snapshot["workers"])
        known_ids = {w["identity"]["initial_item_id"] for w in commands}
        agents = []
        if owner != snapshot["binding"]["conversation_id"]:
            agents = [agent_observation(thread, t, turn_items(entries, t["id"]), chain) for t in thread["turns"]]
        return dict(workers=commands + agents, complete=terminal_ids.issubset(known_ids),
                    turns=[dict(thread_id=owner, turn_id=t["id"]) for t in thread["turns"]],
                    terminals=[dict(kind="command", thread_id=owner, initial_item_id=item) for item in terminal_ids])

    async def scan(self, rpc, snapshot, *, seed=False):
        root = snapshot["binding"]["conversation_id"]
        ancestry = await self.discover(rpc, root)
        inventories = []
        for owner, chain in ancestry.items():
            inventories.append(await self.owner_inventory(rpc, snapshot, owner, chain))
        workers = [w for inventory in inventories for w in inventory["workers"]]
        scopes = [dict(thread_id=owner, turn_id=None, cursor=None, complete=True) for owner in ancestry]
        mark = scan_checkpoint(snapshot, inventories, scopes, seed)
        complete = seed or all(inventory["complete"] for inventory in inventories)
        return dict(type="inventory", certainty="known" if complete else "unknown", workers=workers,
                    observed_revision=snapshot["revision"], history_checkpoint=mark)


def incomplete_turn_history(thread, entries):
    known_turns = {turn["id"] for turn in thread["turns"]}
    missing_owner = not all(entry["turnId"] in known_turns for entry in entries)
    return missing_owner or missing_current_turn(thread)


def missing_current_turn(thread):
    expected = {"active": "inProgress", "systemError": "failed"}.get(thread["status"]["type"])
    turns = thread["turns"]
    return expected is not None and (not turns or turns[-1]["status"] != expected)


def turn_items(entries, turn):
    return [entry["item"] for entry in entries if entry["turnId"] == turn]


def initial_checkpoint(snapshot, inventories, scopes):
    workers = [w["identity"] for inventory in inventories for w in inventory["workers"]]
    terminals = [w for inventory in inventories for w in inventory["terminals"]]
    turns = [t for inventory in inventories for t in inventory["turns"]]
    return dict(launch_id=snapshot["binding"]["launch_id"], seeded=True, baseline_turns=turns,
                baseline_workers=unique_identities(workers + terminals), seen_completions=[], scopes=scopes)


def scan_checkpoint(snapshot, inventories, scopes, seed):
    if seed:
        return initial_checkpoint(snapshot, inventories, scopes)
    mark = deepcopy(snapshot.get("history_checkpoint"))
    if mark:
        mark["scopes"] = scopes
    return mark


def stored_commands(workers, owner):
    return {w["identity"]["initial_item_id"]: deepcopy(w) for w in workers
            if w["identity"]["kind"] == "command" and w["identity"]["thread_id"] == owner}


def unique_identities(identities):
    return list({identity_key(identity): identity for identity in identities}.values())


def merge_history_entries(entries, turns, *, messages_only=False):
    """Keep safe same-turn messages from every successful view in this scan.

    Command entries remain anchored by the existing item/owning-turn checks.
    A later full view supplements messages rather than overwriting native output.
    """
    merged = {(e['turnId'], e['item']['id']): e for e in entries}
    for turn in turns:
        for item in turn['items']:
            entry = dict(turnId=turn['id'], item=item)
            require(native_item(entry), 'Unreadable full turn items')
            if messages_only and item['type'] != 'agentMessage':
                continue
            key = (turn['id'], item['id'])
            if key not in merged:
                merged[key] = entry
    return list(merged.values())
