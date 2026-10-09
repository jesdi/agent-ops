"""Listener-owned snapshots of a task's authoritative main-turn lifecycle."""

from copy import deepcopy
import time
from uuid import uuid4

from dispatcher.runtime_snapshots import (number, read_current, task_path, text,
                                          valid_native_workers, valid_snapshot, write_snapshot)
from dispatcher.runtime_http import RuntimeClient  # public host client
from dispatcher.runtime_inventory import apply_inventory, report_wait
from dispatcher.runtime_bootstrap import record_initial_start
from dispatcher.runtime_work import current_stop_follows_inputs, inputs_resolved, running_workers
from dispatcher.runtime_delivery import apply_delivery, results_resolved
from dispatcher.runtime_bootstrap import attempt_root, bind_root, send_initial
from dispatcher.runtime_input import valid_native_input
from dispatcher.runtime_history import accept_history, settle_history


def _bind(snapshot, event):
    return bind_root(snapshot, event)


def _service(snapshot, event):
    status = event.get("status")
    if status not in ("unknown", "live", "dead"):
        return False
    if snapshot['service'] == 'dead' and status != 'dead':
        return False
    snapshot["service"] = status
    return True


def _main_turn(snapshot, event):
    conversation = snapshot["binding"]["conversation_id"]
    turn = event.get("turn_id")
    return (isinstance(conversation, str) and bool(conversation)
            and event.get("thread_id") == conversation
            and isinstance(turn, str) and bool(turn))


def _start_turn(snapshot, event, *, recovered=False):
    if not _main_turn(snapshot, event):
        return False
    main = snapshot["main"]
    turn = event["turn_id"]
    if turn in main["seen_turns"]:
        return False
    main["status"] = "active"
    main["turn_id"] = turn
    main["seen_turns"].append(turn)
    if not recovered:
        record_initial_start(snapshot, turn)
    return True


def _recover_turn(snapshot, event):
    if not _main_turn(snapshot, event) or event.get("status") != "inProgress":
        return False
    main = snapshot["main"]
    turn = event["turn_id"]
    if turn not in main["seen_turns"]:
        return _start_turn(snapshot, event, recovered=True)
    if main["status"] == "active" and main["turn_id"] == turn:
        return not inputs_resolved(snapshot)
    if main["status"] != "unknown" or main["seen_turns"][-1] != turn:
        return False
    main.update(status="active", turn_id=turn)
    return True


def _complete_turn(snapshot, event):
    if not _main_turn(snapshot, event) or event.get("status") != "completed":
        return False
    main = snapshot["main"]
    if main["status"] != "active" or main["turn_id"] != event["turn_id"]:
        return False
    main["status"] = "stopped"
    main["completed_turns"][event["turn_id"]] = snapshot["revision"] + 1
    for receipt in snapshot["inputs"].values():
        if receipt["status"] == "accepted" and receipt["turn_id"] == event["turn_id"]:
            receipt["status"] = "settled"
    return True


def _control_unknown(snapshot, event):
    message = event.get("message")
    if not isinstance(message, str) or not message:
        return False
    snapshot["inventory"] = "unknown"
    snapshot["main"].update(status="unknown", turn_id=None)
    alert = {"kind": "compatibility", "message": message}
    if alert not in snapshot["alerts"]:
        snapshot["alerts"].append(alert)
    return True


def _input_receipt(snapshot, event):
    identity = event.get("client_message_id")
    if not text(identity):
        return False
    receipt = snapshot["inputs"].get(identity)
    if receipt is None or receipt["status"] != "pending":
        return False
    if event["type"] == "input/rejected":
        receipt["status"] = "rejected"
        return True
    turn = event.get("turn_id")
    if not text(turn):
        return False
    expected = receipt.get('native_input', {}).get('expected_turn_id')
    if expected is not None and turn != expected:
        return False
    receipt.update(status="accepted", turn_id=turn)
    main = snapshot["main"]
    if main["completed_turns"].get(turn, -1) > receipt["revision"]:
        receipt["status"] = "settled"
    return True


_EVENTS = {"input/accepted": _input_receipt, "input/rejected": _input_receipt,
           "input/history": accept_history, "input/history-settled": settle_history,
           "bootstrap/root-attempted": attempt_root, "bootstrap/sent": send_initial,
           "control/unknown": _control_unknown, "turn/recovered": _recover_turn,
           "bound": _bind, "service": _service,
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
    report_wait(snapshot, identities, now)


def _apply_event(snapshot, event, now):
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return False
    if event_type == "inventory":
        return apply_inventory(snapshot, event, time.time() if now is None else now)
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
            "main": {"status": "unknown", "turn_id": None, "seen_turns": [], "completed_turns": {}},
            "inventory": "unknown", "workers": [], "completions": [],
            "deliveries": [], "wait": None, "alerts": [], "retired": False,
            "inputs": {},
        }
        if runtime == 'codex':
            snapshot['bootstrap'] = None
            snapshot['history_checkpoint'] = None
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
        return self._apply_current_event(snapshot, event, now)

    def _apply_current_event(self, snapshot, event, now):
        if snapshot['service'] == 'dead' and event.get('type') != 'service':
            return False
        before = deepcopy(snapshot)
        delivery = str(event.get("type", "")).startswith("delivery/")
        if delivery:
            accepted = apply_delivery(snapshot, event, self.state_dir)
        else:
            accepted = _apply_event(snapshot, event, now)
        if not accepted:
            return False
        replay = delivery or _replayable_event(event)
        return (replay and snapshot == before) or self._save(snapshot)

    def _save(self, snapshot):
        binding = snapshot["binding"]
        snapshot["revision"] += 1
        if not valid_snapshot(snapshot):
            return False
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
        if not isinstance(binding, dict) or type(revision) is not int:
            return "unknown"
        snapshot = self.view(binding.get("target"), binding.get("issue"))
        if not valid_snapshot(snapshot) or snapshot["binding"] != binding:
            return "unknown"
        if snapshot["retired"]:
            return "retired"
        if not _retirement_eligible(snapshot, revision, reason, now, cap):
            return "held"
        snapshot["retired"] = True
        return "retired" if self._save(snapshot) else "unknown"

    def accept_input(self, binding, client_message_id, *, native_input=None) -> bool:
        if not isinstance(binding, dict) or not text(client_message_id):
            return False
        snapshot = self._current(binding)
        if snapshot is None or snapshot['service'] == 'dead' or client_message_id in snapshot["inputs"]:
            return False
        if not _valid_input_provenance(binding, native_input):
            return False
        snapshot["inputs"][client_message_id] = {
            "status": "pending", "turn_id": None, "revision": snapshot["revision"] + 1}
        if native_input is not None:
            snapshot['inputs'][client_message_id]['native_input'] = deepcopy(native_input)
        return self._save(snapshot)


def _replayable_event(event):
    return (event.get('type') in ('bootstrap/root-attempted', 'bootstrap/sent', 'bound',
                                 'input/history', 'input/history-settled')
            or (event.get('type') == 'service' and event.get('status') == 'dead'))


def _valid_input_provenance(binding, native_input):
    return (native_input is None or (binding['runtime'] == 'codex'
            and valid_native_input(native_input, binding['conversation_id'])))


def _retirement_eligible(snapshot, revision, reason, now, cap):
    if reason == "forced":
        return True
    permitted = all((snapshot["revision"] == revision,
                snapshot["service"] == "live", snapshot["main"]["status"] == "stopped",
                snapshot["inventory"] == "known", results_resolved(snapshot),
                inputs_resolved(snapshot), current_stop_follows_inputs(snapshot)))
    return permitted and _retirement_work(snapshot, reason, now, cap)


def _retirement_work(snapshot, reason, now, cap):
    running = running_workers(snapshot)
    if reason == "stopped":
        return not running and all(w.get("status") not in ("running", "unknown") for w in snapshot["workers"])
    if reason != "background" or not running or snapshot["wait"] is None:
        return False
    now = time.time() if now is None else now
    return _past_cap(snapshot["wait"], now, cap)


def _past_cap(wait, now, cap):
    return number(now) and number(cap) and cap >= 0 and now - wait["since"] > cap
