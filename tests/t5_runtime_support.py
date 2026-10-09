"""Independent T5 public-contract observations; no product logic implementation."""

from copy import deepcopy

from dispatcher.runtime_control import RuntimeControl


def covers(*identifiers):
    def decorate(function):
        function.t5_criteria = identifiers
        return function
    return decorate


class RuntimeCase:
    def __init__(self, directory, *, runtime="codex", issue=501, root="root-fixture", stage="review", ticket="", control=None, worktree=""):
        self.directory = directory
        self.root = root
        self.control = RuntimeControl(directory) if control is None else control
        self.binding = self.control.prepare("fixture", issue, stage, runtime=runtime,
                                            conversation_id=root, ticket=ticket, worktree=worktree)["binding"]
        self.send({"type": "service", "status": "live"})
        self.start("turn-A")
        self.inventory([])
        self.stops = {}

    def view(self):
        return self.control.view(self.binding["target"], self.binding["issue"], self.binding["launch_id"])

    def send(self, event, *, now=100):
        return self.control.event(self.binding, event, now=now)

    def start(self, turn, *, now=100):
        return self.send({"type": "turn/started", "thread_id": self.root, "turn_id": turn}, now=now)

    def stop(self, turn="turn-A", *, now=100, background_tasks=None):
        event = {"type": "turn/completed", "thread_id": self.root, "turn_id": turn, "status": "completed"}
        if background_tasks is not None:
            event["background_tasks"] = background_tasks
        accepted = self.send(event, now=now)
        if accepted:
            self.stops[turn] = {"thread_id": self.root, "turn_id": turn, "status": "completed",
                                "revision": self.view()["revision"]}
        return accepted

    def inventory(self, workers, *, now=100, certainty="known", revision="current", checkpoint=None):
        event = {"type": "inventory", "certainty": certainty, "workers": deepcopy(workers)}
        if revision == "current":
            event["observed_revision"] = self.view()["revision"]
        elif revision is not None:
            event["observed_revision"] = revision
        if checkpoint is not None:
            event["history_checkpoint"] = deepcopy(checkpoint)
        if certainty == "unknown":
            event["message"] = "fixture authoritative inventory unavailable"
        return self.send(event, now=now)

    def retire(self, *, reason="background", now=20000, cap=10800, revision=None):
        return self.control.retire(self.binding, self.view()["revision"] if revision is None else revision,
                                   reason=reason, now=now, cap=cap)


def chain(root, depth=0, leaf=None):
    names = [root] + ["descendant-" + str(i) for i in range(1, depth + 1)]
    if leaf is not None and depth:
        names[-1] = leaf
    result = [{"thread_id": root, "parent_thread_id": None, "source_kind": "bound-root",
               "source_parent_thread_id": None, "depth": 0}]
    for number in range(1, len(names)):
        result.append({"thread_id": names[number], "parent_thread_id": names[number - 1],
                       "source_kind": "thread-spawn", "source_parent_thread_id": names[number - 1],
                       "depth": number})
    return list(reversed(result))


def command(case, item="exec-A", *, owner=None, turn="turn-A", depth=0, normal_stop="recorded", source="unifiedExecStartup"):
    owner = case.root if owner is None else owner
    if normal_stop == "recorded":
        normal_stop = deepcopy(case.stops.get(turn)) if owner == case.root else None
    return {"identity": {"kind": "command", "thread_id": owner, "initial_item_id": item},
            "turn_id": turn, "ancestry": chain(case.root, depth, owner), "process_id": "process-shared",
            "command": "generic fixture command", "cwd": "/tmp/fixture-workspace", "source": source,
            "status": "running", "inventory_running": True, "normal_stop": normal_stop, "outcome": None}


def agent(case, *, owner="child-fixture", turn="child-turn-A", depth=1, flags=()):
    return {"identity": {"kind": "agent", "thread_id": owner, "turn_id": turn},
            "ancestry": chain(case.root, depth, owner), "thread_status": "active",
            "active_flags": list(flags), "status": "running", "outcome": None}


def finished(observation, *, status="completed", exit_code=0, output="AVAILABLE_SUFFIX", duration=12, messages=None, error=None):
    result = deepcopy(observation)
    result["status"] = status
    if result["identity"]["kind"] == "command":
        result["inventory_running"] = False
        result["outcome"] = {"status": status, "exit_code": exit_code, "aggregated_output": output, "duration_ms": duration}
    else:
        result["thread_status"] = "systemError" if status == "failed" else "idle"
        result["outcome"] = {"status": status, "messages": messages or [], "error": error}
    return result


def stored(snapshot, identity, *, optional=False):
    worker = next((worker for worker in snapshot["workers"] if worker["identity"] == identity), None)
    if not optional:
        assert worker is not None, f"declared observed worker was not retained: {identity}"
    return worker


def completion(snapshot, identity, *, optional=False):
    result = next((record for record in snapshot["completions"] if record["identity"] == identity), None)
    if not optional:
        assert result is not None, f"eligible available completion missing: {identity}"
    return result


def qualified(case, *items, now=100):
    case.stop(now=now)
    observations = [command(case, item) for item in (items or ("exec-A",))]
    case.inventory(observations, now=now)
    for observation in observations:
        assert stored(case.view(), observation["identity"])["eligible"], "authoritative post-Stop survival did not qualify work"
    assert case.view()["wait"] is not None, "stopped report of eligible running work did not start wait clock"
    return observations


def checkpoint(case, *, seeded=True, baseline_turns=(), baseline_workers=(), seen=(), scopes=()):
    return {"launch_id": case.binding["launch_id"], "seeded": seeded,
            "baseline_turns": list(baseline_turns), "baseline_workers": deepcopy(list(baseline_workers)),
            "seen_completions": deepcopy(list(seen)), "scopes": deepcopy(list(scopes))}


def native_worker(identity):
    return {"id": identity, "type": "shell", "status": "running", "command": "fixture-command",
            "description": "generic fixture work"}


def identities(values):
    """Identity union assertions have no incidental list-order requirement."""
    import json
    return {json.dumps(value, sort_keys=True) for value in values}
