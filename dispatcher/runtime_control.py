"""Listener-owned snapshots of a task's authoritative main-turn lifecycle."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4


def _snapshot_path(state_dir, target, issue):
    key = hashlib.sha256(f"{target}\0{issue}".encode()).hexdigest()
    return Path(state_dir) / "runtime" / f"{key}.json"


def _write_snapshot(path, snapshot):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    temporary.replace(path)


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


def _apply_event(snapshot, event):
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return False
    handler = _EVENTS.get(event_type)
    return handler is not None and handler(snapshot, event)


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
        _write_snapshot(_snapshot_path(self.state_dir, target, issue), snapshot)
        return snapshot

    def view(self, target, issue, launch_id=None) -> dict | None:
        try:
            snapshot = json.loads(_snapshot_path(self.state_dir, target, issue)
                                  .read_text(encoding="utf-8"))
            if snapshot["version"] != 1:
                return None
            if launch_id is not None and snapshot["binding"]["launch_id"] != launch_id:
                return None
            return snapshot
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def event(self, binding, event, *, now=None) -> bool:
        if not isinstance(binding, dict) or not isinstance(event, dict):
            return False
        snapshot = self.view(binding.get("target"), binding.get("issue"))
        if snapshot is None or snapshot["binding"] != binding or snapshot["retired"]:
            return False
        if not _apply_event(snapshot, event):
            return False
        snapshot["revision"] += 1
        _write_snapshot(_snapshot_path(self.state_dir, binding["target"], binding["issue"]),
                        snapshot)
        return True

    def retire(self, binding, revision, *, reason="stopped", now=None,
               cap=10800) -> str:
        raise NotImplementedError("RuntimeControl.retire lifecycle behavior is missing")

    def accept_input(self, binding, client_message_id) -> bool:
        raise NotImplementedError("RuntimeControl.accept_input lifecycle behavior is missing")
