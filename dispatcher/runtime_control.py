"""Listener-owned snapshots of a task's authoritative main-turn lifecycle."""

import time
from uuid import uuid4

from dispatcher.runtime_snapshots import (number, read_current, task_path, valid_native_workers,
                                          valid_snapshot, write_snapshot)
from dispatcher.runtime_http import RuntimeClient  # public host client


def _bind(snapshot, event):
    conversation = event.get("conversation_id")
    if snapshot["binding"]["conversation_id"] is not None:
        return False
    if not isinstance(conversation, str) or not conversation:
        return False
    snapshot["binding"]["conversation_id"] = conversation
    return True


def _service(snapshot, event):
    status = event.get("status")
    if status not in ("unknown", "live", "dead"):
        return False
    snapshot["service"] = status
    return True


def _empty_inventory(snapshot, event):
    # Owned-worker reconciliation belongs to the controller integration.
    if event.get("certainty") != "known" or event.get("workers") != []:
        return False
    snapshot["inventory"] = "known"
    snapshot["workers"] = []
    return True


def _main_turn(snapshot, event):
    conversation = snapshot["binding"]["conversation_id"]
    turn = event.get("turn_id")
    return (isinstance(conversation, str) and bool(conversation)
            and event.get("thread_id") == conversation
            and isinstance(turn, str) and bool(turn))


def _start_turn(snapshot, event):
    if not _main_turn(snapshot, event):
        return False
    main = snapshot["main"]
    turn = event["turn_id"]
    if turn in main["seen_turns"]:
        return False
    main["status"] = "active"
    main["turn_id"] = turn
    main["seen_turns"].append(turn)
    return True


def _complete_turn(snapshot, event):
    if not _main_turn(snapshot, event) or event.get("status") != "completed":
        return False
    main = snapshot["main"]
    if main["status"] != "active" or main["turn_id"] != event["turn_id"]:
        return False
    main["status"] = "stopped"
    return True


_EVENTS = {"bound": _bind, "service": _service, "inventory": _empty_inventory,
           "turn/started": _start_turn, "turn/completed": _complete_turn}


def _native_inventory(snapshot, event, now):
    workers = event.get("background_tasks")
    if not valid_native_workers(workers):
        snapshot["inventory"] = "unknown"
        return
    snapshot["inventory"] = "known"
    snapshot["workers"] = workers
    identities = sorted({w["id"] for w in workers})
    if not identities:
        return
    previous = snapshot["wait"]
    since = previous["since"] if previous and set(identities).issubset(previous["workers"]) else now
    snapshot["wait"] = {"since": since, "workers": identities}


def _apply_event(snapshot, event, now):
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return False
    handler = _EVENTS.get(event_type)
    if handler is None or not handler(snapshot, event):
        return False
    if event_type == "turn/completed" and snapshot["binding"]["runtime"] == "claude":
        _native_inventory(snapshot, event, time.time() if now is None else now)
    return True


class RuntimeControl:
    """The listener-owned, launch-bound snapshot service."""

    def __init__(self, state_dir):
        self.state_dir = state_dir

    def prepare(self, target, issue, stage, *, ticket="", runtime="codex",
                conversation_id=None, worktree="") -> dict:
        snapshot = {
            "version": 1,
            "binding": {"target": target, "issue": issue, "stage": stage,
                        "ticket": ticket, "runtime": runtime,
                        "conversation_id": conversation_id, "worktree": worktree,
                        "launch_id": str(uuid4())},
            "revision": 0, "service": "unknown",
            "main": {"status": "unknown", "turn_id": None, "seen_turns": []},
            "inventory": "unknown", "workers": [], "completions": [],
            "deliveries": [], "wait": None, "alerts": [], "retired": False,
        }
        if not valid_snapshot(snapshot):
            raise ValueError("invalid launch binding")
        directory = task_path(self.state_dir, target, issue)
        launch_id = snapshot["binding"]["launch_id"]
        write_snapshot(directory / f"{launch_id}.json", snapshot)
        write_snapshot(directory / "current.json", {"launch_id": launch_id})
        return snapshot

    def view(self, target, issue, launch_id=None) -> dict | None:
        return read_current(task_path(self.state_dir, target, issue), target, issue, launch_id)

    def event(self, binding, event, *, now=None) -> bool:
        if not isinstance(binding, dict) or not isinstance(event, dict):
            return False
        if now is not None and not number(now):
            return False
        snapshot = self._current(binding)
        if snapshot is None:
            return False
        if not _apply_event(snapshot, event, now) or not valid_snapshot(snapshot):
            return False
        snapshot["revision"] += 1
        directory = task_path(self.state_dir, binding["target"], binding["issue"])
        write_snapshot(directory / f"{binding['launch_id']}.json", snapshot)
        return True

    def _current(self, binding):
        snapshot = self.view(binding.get("target"), binding.get("issue"))
        if not valid_snapshot(snapshot) or snapshot["binding"] != binding or snapshot["retired"]:
            return None
        return snapshot

    def retire(self, binding, revision, *, reason="stopped", now=None,
               cap=10800) -> str:
        raise NotImplementedError("RuntimeControl.retire lifecycle behavior is missing")

    def accept_input(self, binding, client_message_id) -> bool:
        raise NotImplementedError("RuntimeControl.accept_input lifecycle behavior is missing")
