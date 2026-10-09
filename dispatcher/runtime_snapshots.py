"""Atomic host-only runtime storage and conservative schema validation."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from uuid import UUID


def task_path(state_dir, target, issue):
    key = hashlib.sha256(f"{target}\0{issue}".encode()).hexdigest()
    return Path(state_dir) / "runtime" / key


def _sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(directory):
    if not directory.is_dir():
        _ensure_directory(directory.parent)
        directory.mkdir(exist_ok=True)
    # Also resync existing entries: a prior failed prepare may have created
    # this directory but failed before its parent entry became durable.
    _sync_directory(directory.parent)


def write_snapshot(path, snapshot):
    _ensure_directory(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".snapshot-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def text(value):
    return isinstance(value, str) and bool(value)


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def valid_binding(binding):
    if not isinstance(binding, dict):
        return False
    strings = ("target", "stage", "launch_id", "runtime")
    if not all(text(binding.get(key)) for key in strings):
        return False
    if not all(isinstance(binding.get(key), str) for key in ("ticket", "worktree")):
        return False
    return all((type(binding.get("issue")) is int, binding.get("issue", 0) > 0,
                binding["runtime"] in ("claude", "codex"), "conversation_id" in binding,
                binding.get("conversation_id") is None or text(binding["conversation_id"])))


def valid_main(main):
    if not isinstance(main, dict):
        return False
    if main.get("status") not in ("unknown", "active", "stopped"):
        return False
    if not valid_turn_history(main):
        return False
    if main["status"] == "unknown":
        return "turn_id" in main and main["turn_id"] is None
    return valid_current_turn(main)


def valid_wait(wait):
    if wait is None:
        return True
    if not isinstance(wait, dict) or not number(wait.get("since")):
        return False
    return isinstance(wait.get("workers"), list) and all(text(w) for w in wait["workers"])


def valid_native_worker(worker):
    return (isinstance(worker, dict) and text(worker.get("id"))
            and worker.get("status") == "running")


def valid_native_workers(workers):
    return isinstance(workers, list) and all(valid_native_worker(worker) for worker in workers)


def _supported_workers(snapshot):
    workers = snapshot.get("workers")
    if snapshot["binding"]["runtime"] == "codex":
        return workers == []  # Codex worker schemas belong to the inventory integration.
    return valid_native_workers(workers)


def valid_status_fields(snapshot):
    scalar = (type(snapshot.get("revision")) is int, snapshot.get("revision", -1) >= 0,
              snapshot.get("service") in ("unknown", "live", "dead"),
              snapshot.get("inventory") in ("unknown", "known"),
              type(snapshot.get("retired")) is bool)
    return all(scalar)


def valid_alerts(alerts):
    return (isinstance(alerts, list) and all(
        isinstance(alert, dict) and alert.get("kind") == "compatibility"
        and text(alert.get("message")) for alert in alerts))


def valid_snapshot(snapshot):
    if not isinstance(snapshot, dict) or type(snapshot.get("version")) is not int or snapshot["version"] != 1:
        return False
    if not valid_binding(snapshot.get("binding")) or not valid_main(snapshot.get("main")):
        return False
    # Later worker/delivery tickets must explicitly extend these reserved schemas.
    reserved = ("completions", "deliveries")
    fields_valid = all((valid_status_fields(snapshot), _supported_workers(snapshot),
                all(snapshot.get(key) == [] for key in reserved),
                valid_alerts(snapshot.get("alerts")), valid_inputs(snapshot.get("inputs")),
                "wait" in snapshot, valid_wait(snapshot.get("wait"))))
    return fields_valid and valid_provenance(snapshot)


def unknown_view():
    return {"main": {"status": "unknown", "turn_id": None}, "inventory": "unknown",
            "service": "unknown", "binding": {}, "workers": [], "wait": None}


def read_current(directory, target, issue, launch_id=None):
    pointer = directory / "current.json"
    try:
        try:
            pointer.lstat()
        except FileNotFoundError:
            return _missing_current(directory)
        current = json.loads(pointer.read_text(encoding="utf-8"))
        selected = current["launch_id"]
        UUID(selected)
        if launch_id is not None and selected != launch_id:
            return None
        snapshot = json.loads((directory / f"{selected}.json").read_text(encoding="utf-8"))
        if not valid_snapshot(snapshot):
            return unknown_view()
        binding = snapshot["binding"]
        if (binding["target"], binding["issue"], binding["launch_id"]) != (target, issue, selected):
            return unknown_view()
        return snapshot
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return unknown_view()


def _missing_current(directory):
    # An interrupted prepare or a lost pointer cannot resurrect legacy evidence.
    # lstat propagates permission failures to read_current's unknown fallback.
    try:
        directory.lstat()
    except FileNotFoundError:
        return None
    return unknown_view()


def valid_inputs(inputs):
    return isinstance(inputs, dict) and all(
        text(identity) and valid_input(receipt) for identity, receipt in inputs.items())


def valid_input(receipt):
    if not isinstance(receipt, dict):
        return False
    return all((receipt.get("status") in ("pending", "accepted", "settled", "rejected"),
                "turn_id" in receipt,
                receipt.get("turn_id") is None or text(receipt["turn_id"]),
                type(receipt.get("revision")) is int, receipt.get("revision", -1) >= 0))


def valid_completed_turns(turns):
    return isinstance(turns, dict) and all(
        text(turn) and type(revision) is int and revision >= 0
        for turn, revision in turns.items())


def valid_turn_history(main):
    seen = main.get("seen_turns")
    return (valid_completed_turns(main.get("completed_turns"))
            and isinstance(seen, list) and all(text(turn) for turn in seen)
            and len(seen) == len(set(seen)))


def valid_current_turn(main):
    turn = main.get("turn_id")
    seen = main["seen_turns"]
    if not text(turn) or not seen or turn != seen[-1]:
        return False
    return (turn in main["completed_turns"]) == (main["status"] == "stopped")


def valid_provenance(snapshot):
    if not valid_conversation_provenance(snapshot):
        return False
    main = snapshot["main"]
    ends = main["completed_turns"]
    if not all(turn in main["seen_turns"] and 0 < revision <= snapshot["revision"]
               for turn, revision in ends.items()):
        return False
    return all(valid_input_provenance(receipt, ends, snapshot["revision"])
               for receipt in snapshot["inputs"].values())


def valid_input_provenance(receipt, ends, revision):
    if not 0 < receipt["revision"] <= revision:
        return False
    turn = receipt["turn_id"]
    if receipt["status"] in ("pending", "rejected"):
        return turn is None
    if not text(turn):
        return False
    if receipt["status"] == "settled":
        return ends.get(turn, -1) > receipt["revision"]
    # An accepted ACK may arrive before its turn/started notification.
    return True


def valid_conversation_provenance(snapshot):
    if snapshot["binding"]["conversation_id"] is not None:
        return True
    return (not snapshot["main"]["seen_turns"]
            and all(receipt["turn_id"] is None for receipt in snapshot["inputs"].values()))
