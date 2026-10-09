"""Declared owned-work values and their launch-scoped validation."""
import json


def nonempty(value):
    return isinstance(value, str) and bool(value)


def identity_key(identity):
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def valid_identity(identity):
    if not isinstance(identity, dict) or not nonempty(identity.get("thread_id")):
        return False
    field = "initial_item_id" if identity.get("kind") == "command" else "turn_id"
    if identity.get("kind") not in ("command", "agent"):
        return False
    return field is not None and set(identity) == {"kind", "thread_id", field} and nonempty(identity[field])


def valid_node(node, owner, depth):
    return (isinstance(node, dict) and node.get("thread_id") == owner
            and type(node.get("depth")) is int and node["depth"] == depth)


def valid_root(node, root):
    return all((node["thread_id"] == root, node.get("source_kind") == "bound-root",
                node.get("parent_thread_id") is None, node.get("source_parent_thread_id") is None))


def valid_link(node):
    parent = node.get("parent_thread_id")
    return (nonempty(parent) and node.get("source_kind") == "thread-spawn"
            and node.get("source_parent_thread_id") == parent)


def valid_ancestry(nodes, owner, root):
    if not isinstance(nodes, list) or not nodes:
        return False
    expected, seen = owner, set()
    for depth, node in zip(range(len(nodes) - 1, -1, -1), nodes):
        if not valid_node(node, expected, depth) or expected in seen:
            return False
        seen.add(expected)
        if depth == 0:
            return valid_root(node, root)
        if not valid_link(node):
            return False
        expected = node["parent_thread_id"]
    return False


def nullable(value, kind):
    return value is None or type(value) is kind


def valid_command_outcome(outcome):
    return all((outcome.get("status") in ("completed", "failed", "declined"),
                nullable(outcome.get("exit_code"), int), nullable(outcome.get("aggregated_output"), str),
                nullable(outcome.get("duration_ms"), int),
                {"status", "exit_code", "aggregated_output", "duration_ms"} <= outcome.keys()))


def valid_agent_outcome(outcome):
    messages, error = outcome.get("messages"), outcome.get("error")
    return all((outcome.get("status") in ("completed", "failed", "interrupted"),
                isinstance(messages, list) and all(isinstance(m, dict) and nonempty(m.get("item_id"))
                    and isinstance(m.get("text"), str) for m in messages),
                "error" in outcome,
                error is None or isinstance(error, dict) and isinstance(error.get("message"), str)))


def valid_outcome(outcome, kind):
    if not isinstance(outcome, dict):
        return False
    return valid_command_outcome(outcome) if kind == "command" else valid_agent_outcome(outcome)


def valid_command(worker):
    fields = {"turn_id", "process_id", "command", "cwd", "source", "inventory_running", "normal_stop", "outcome"}
    return all((fields <= worker.keys(), nonempty(worker.get("turn_id")), nullable(worker.get("process_id"), str),
                isinstance(worker.get("command"), str), isinstance(worker.get("cwd"), str),
                worker.get("source") in (None, "agent", "userShell", "unifiedExecStartup", "unifiedExecInteraction"),
                type(worker.get("inventory_running")) is bool,
                worker.get("status") in ("running", "completed", "failed", "declined", "unknown"),
                "normal_stop" in worker))


def valid_agent(worker):
    flags = worker.get("active_flags")
    return all((worker.get("thread_status") in ("active", "idle", "systemError", "notLoaded"),
                isinstance(flags, list) and all(f in ("waitingOnApproval", "waitingOnUserInput") for f in flags),
                worker.get("status") in ("running", "completed", "failed", "interrupted", "unknown")))


def valid_observation(worker, root):
    if not isinstance(worker, dict) or not valid_identity(worker.get("identity")):
        return False
    identity = worker["identity"]
    if not valid_ancestry(worker.get("ancestry"), identity["thread_id"], root):
        return False
    if not valid_worker_outcome(worker, identity["kind"]):
        return False
    return valid_command(worker) if identity["kind"] == "command" else identity["thread_id"] != root and valid_agent(worker)


def valid_worker_outcome(worker, kind):
    if "outcome" not in worker:
        return False
    outcome = worker["outcome"]
    if outcome is None:
        return worker.get("status") in ("running", "unknown")
    return valid_outcome(outcome, kind) and outcome["status"] == worker.get("status")


def matching_stop(worker, stop):
    return (isinstance(stop, dict) and stop.get("status") == "completed"
            and stop.get("thread_id") == worker["identity"]["thread_id"]
            and stop.get("turn_id") == worker["turn_id"])


def valid_stop(worker, stop, snapshot, observed_revision):
    if not matching_stop(worker, stop):
        return False
    if stop["thread_id"] != snapshot["binding"]["conversation_id"]:
        return stop.get("evidence_source") in ("native-child-event", "authoritative-child-history")
    revision = stop.get("revision")
    return (type(revision) is int and type(observed_revision) is int
            and 0 < revision <= observed_revision <= snapshot["revision"]
            and snapshot["main"]["completed_turns"].get(stop["turn_id"]) == revision)


def valid_stored_worker(worker, snapshot):
    if not valid_observation(worker, snapshot["binding"]["conversation_id"]) or type(worker.get("eligible")) is not bool:
        return False
    if worker["identity"]["kind"] != "command":
        return True
    if not worker["eligible"]:
        return worker.get("qualifying_stop") is None
    return valid_stop(worker, worker.get("qualifying_stop"), snapshot, snapshot["revision"])


def valid_completion(value, workers):
    if not isinstance(value, dict) or not valid_identity(value.get("identity")):
        return False
    worker = workers.get(identity_key(value["identity"]))
    if worker is None:
        return False
    return all((worker["eligible"], worker["outcome"] is not None,
                value.get("outcome") == worker["outcome"]))


def valid_completions(snapshot):
    values = snapshot.get("completions")
    if not isinstance(values, list):
        return False
    workers = {identity_key(w["identity"]): w for w in snapshot["workers"]}
    if not all(valid_completion(value, workers) for value in values):
        return False
    return len({identity_key(v["identity"]) for v in values}) == len(values)


def valid_scope(scope):
    return (isinstance(scope, dict) and nonempty(scope.get("thread_id"))
            and "turn_id" in scope and nullable(scope["turn_id"], str)
            and "cursor" in scope and nullable(scope["cursor"], str)
            and type(scope.get("complete")) is bool)


def valid_turn_identity(value):
    return isinstance(value, dict) and nonempty(value.get("thread_id")) and nonempty(value.get("turn_id"))


def valid_checkpoint(checkpoint, launch):
    if checkpoint is None:
        return True
    if not isinstance(checkpoint, dict) or checkpoint.get("launch_id") != launch or type(checkpoint.get("seeded")) is not bool:
        return False
    validators = {"baseline_turns": valid_turn_identity, "baseline_workers": valid_identity,
                  "seen_completions": valid_identity, "scopes": valid_scope}
    return all(isinstance(checkpoint.get(field), list) and all(validate(v) for v in checkpoint[field])
               for field, validate in validators.items())


def running_workers(snapshot):
    if snapshot["binding"]["runtime"] == "claude":
        return snapshot["workers"]
    return [w for w in snapshot["workers"] if w["status"] == "running" and w["eligible"]]


def inputs_resolved(snapshot):
    return all(r["status"] in ("settled", "rejected") for r in snapshot["inputs"].values())


def current_stop_follows_inputs(snapshot):
    main = snapshot['main']
    end = main['completed_turns'].get(main['turn_id'], -1)
    return all(receipt['status'] == 'rejected' or receipt['revision'] < end
               for receipt in snapshot['inputs'].values())
