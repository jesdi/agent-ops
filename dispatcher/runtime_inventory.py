"""Listener-owned monotonic reconciliation and cumulative stopped-work clocks."""
from copy import deepcopy

from dispatcher.runtime_alerts import find_alert
from dispatcher.runtime_work import (current_stop_follows_inputs, identity_key, running_workers,
    valid_checkpoint, valid_observation, valid_stop)


def previously_reported(previous):
    return list(previous.get("ever_reported", previous["workers"])) if previous else []


def wait_label(identity):
    return identity if isinstance(identity, str) else identity_key(identity)


def report_wait(snapshot, identities, now):
    if not identities:
        return
    previous = snapshot["wait"]
    union = previously_reported(previous)
    new = [identity for identity in identities if identity not in union]
    since = now if new or previous is None else previous["since"]
    labels = [wait_label(identity) for identity in identities]
    snapshot["wait"] = {"since": since, "workers": labels, "ever_reported": union + new}


def merge_checkpoint(snapshot, event):
    if "history_checkpoint" not in event:
        return True
    mark = event["history_checkpoint"]
    if not valid_checkpoint(mark, snapshot["binding"]["launch_id"]) or mark is None:
        return False
    previous = snapshot.get("history_checkpoint")
    if previous and previous["seeded"]:
        if any(mark[field] != previous[field] for field in ("seeded", "baseline_turns", "baseline_workers")):
            return False
    snapshot["history_checkpoint"] = deepcopy(mark)
    return True


def baseline_worker(worker, checkpoint):
    if not checkpoint:
        return False
    identity = worker["identity"]
    turn = worker.get("turn_id", identity.get("turn_id"))
    return (identity in checkpoint["baseline_workers"] or
            {"thread_id": identity["thread_id"], "turn_id": turn} in checkpoint["baseline_turns"])


def qualify(worker, snapshot, event):
    mark = snapshot.get("history_checkpoint")
    if mark and not mark["seeded"]:
        return False
    if worker["identity"]["kind"] == "agent":
        return worker["status"] != "unknown"
    if worker["status"] != "running":
        return False
    return all((event["certainty"] == "known", worker["inventory_running"],
                worker["source"] != "unifiedExecInteraction",
                valid_stop(worker, worker["normal_stop"], snapshot, event.get("observed_revision"))))


def retain_command_identity(worker, previous):
    worker["qualifying_stop"] = previous["qualifying_stop"] if previous else None
    if previous:
        worker["turn_id"] = previous["turn_id"]


def retain_previous(previous, observation):
    if previous["outcome"] is not None:
        return True
    if previous["identity"]["kind"] != "command":
        return False
    return (observation["turn_id"] != previous["turn_id"]
            or observation["source"] == "unifiedExecInteraction")


def merge_worker(previous, observation, snapshot, event):
    if previous and retain_previous(previous, observation):
        return previous
    worker = deepcopy(observation)
    worker["eligible"] = bool(previous and previous["eligible"])
    if worker["identity"]["kind"] == "command":
        retain_command_identity(worker, previous)
    if not worker["eligible"] and qualify(worker, snapshot, event):
        worker["eligible"] = True
        if worker["identity"]["kind"] == "command":
            worker["qualifying_stop"] = deepcopy(worker["normal_stop"])
    return worker


def collect_completions(snapshot):
    present = {identity_key(c["identity"]) for c in snapshot["completions"]}
    for worker in snapshot["workers"]:
        key = identity_key(worker["identity"])
        if worker["eligible"] and worker["outcome"] is not None and key not in present:
            snapshot["completions"].append({"identity": deepcopy(worker["identity"]), "outcome": deepcopy(worker["outcome"])})
            present.add(key)
    checkpoint_completions(snapshot)


def checkpoint_completions(snapshot):
    mark = snapshot.get("history_checkpoint")
    if mark:
        for completion in snapshot["completions"]:
            if completion["identity"] not in mark["seen_completions"]:
                mark["seen_completions"].append(deepcopy(completion["identity"]))


def reconcile_workers(snapshot, event):
    retained = {identity_key(w["identity"]): w for w in snapshot["workers"]}
    observed, complete = set(), event["certainty"] == "known"
    mark = snapshot.get("history_checkpoint")
    for observation in event["workers"]:
        if not valid_observation(observation, snapshot["binding"]["conversation_id"]):
            complete = False
            continue
        if baseline_worker(observation, mark):
            continue
        key = identity_key(observation["identity"])
        observed.add(key)
        retained[key] = merge_worker(retained.get(key), observation, snapshot, event)
    snapshot["workers"] = list(retained.values())
    return complete and resolved_inventory(retained, observed, mark)


def resolved_inventory(retained, observed, mark):
    if mark and not mark["seeded"]:
        return False
    return all(worker["outcome"] is not None or key in observed and resolved_worker(worker)
               for key, worker in retained.items())


def resolved_worker(worker):
    if worker["status"] == "unknown":
        return False
    return worker["status"] != "running" or worker["eligible"]


def report_stopped_work(snapshot, now):
    if all((snapshot["inventory"] == "known", snapshot["main"]["status"] == "stopped", current_stop_follows_inputs(snapshot))):
        report_wait(snapshot, [w["identity"] for w in running_workers(snapshot)], now)


def inventory_alert(snapshot, message):
    if isinstance(message, str) and message:
        alert = {"kind": "compatibility", "message": message}
        if find_alert(snapshot, alert) is None:
            snapshot["alerts"].append(alert)


def apply_inventory(snapshot, event, now):
    if event.get("certainty") not in ("known", "unknown") or not isinstance(event.get("workers"), list):
        return False
    if snapshot["binding"]["runtime"] != "codex" or not merge_checkpoint(snapshot, event):
        return False
    complete = reconcile_workers(snapshot, event)
    snapshot["inventory"] = "known" if complete else "unknown"
    collect_completions(snapshot)
    report_stopped_work(snapshot, now)
    inventory_alert(snapshot, event.get("message"))
    return True
