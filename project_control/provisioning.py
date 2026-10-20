"""Host-side provisioning and sandbox verification of a confirmed operation.

`host` is the box's executor (see tests/test_project_provisioning_verification_acceptance.py).
The record `prov` lives in the operation file and is saved after every effect, so a rerun skips
finished clones and steps and first stops any temporary resource an interrupted run left behind.
A resource counts as removed only when `host.stop` says so.
"""
import posixpath

STEPS = ("setup", "verify", "gate")


class Failed(Exception):
    """A blocker that leaves the project paused and the provisioning resumable."""


def new():
    return dict(state="waiting_for_capacity", blocker="", cleanup=[], live=[], done=[])


def requeue(prov):
    """Repeated confirmation of a failed operation: admit it again, keeping its done steps and live resources."""
    if prov["state"] == "failed":
        prov.update(state="waiting_for_capacity")


def view(prov):
    return {k: prov[k] for k in ("state", "blocker", "cleanup")}


def run(host, plan, prov, slot, save):
    try:
        _release(host, prov, save, "an earlier run")
        _check(host, plan)
        _prepare(host, plan, prov, save)
        for repo in plan["repositories"]:
            _repo(host, repo, prov, slot, save)
    except Failed as blocker:
        prov.update(state="failed", blocker=str(blocker))
    else:
        prov.update(state="passed", blocker="")
    save()


def _check(host, plan):
    """Refuse before anything is created: an unsafe target or a missing definition."""
    loc = plan.get("locations") or {}
    app, target = (posixpath.normpath(loc[k]) if loc.get(k) else "" for k in ("app", "target"))
    if app and target and (target == app or target.startswith(app + "/")):
        raise Failed(f"target {target} is the running application {app}")
    names = ", ".join(r["repo"] for r in plan["repositories"])
    for ref in plan.get("secret_refs", []):
        if not host.has_secret(ref):
            raise Failed(f"{names}: secret {ref} is not defined")
    for r in plan["repositories"]:
        for service in r["compose_services"]:
            if not host.has_service(r["repo"], service):
                raise Failed(f"{r['repo']}: service {service} is not defined")


def _once(prov, key, save, call):
    if key not in prov["done"]:
        call()
        prov["done"].append(key)
        save()


def _prepare(host, plan, prov, save):
    for kind, path in (plan.get("locations") or {}).items():
        if kind != "app":
            _once(prov, f"dir:{kind}", save, lambda: host.ensure_dir(kind, path))
    for r in plan["repositories"]:
        _once(prov, f"clone:{r['repo']}", save, lambda: host.ensure_clone(r["repo"], r["clone_path"]))


def _repo(host, r, prov, slot, save):
    name = r["repo"]
    _step(host, r, "setup", prov, slot, save)
    if all(f"{name}:{s}" in prov["done"] for s in STEPS):
        return
    try:
        # An intent receipt is durable before the external effect: a lost response or a crash
        # leaves it live, and host.stop(intent) must remove whatever that launch created.
        intent = dict(kind="launch", id=f"launch:{name}", repo=name, services=list(r["compose_services"]))
        prov["live"].append(intent)
        save()
        started = host.start_services(name, r["compose_services"], slot)
        prov["live"].remove(intent)
        prov["live"] += started["receipts"]  # recorded before anything can fail or be interrupted
        save()
        if not started["ok"]:
            raise Failed(f"{name}: {started['detail']}")
        _step(host, r, "verify", prov, slot, save)
        _step(host, r, "gate", prov, slot, save)
    finally:
        left = _stop_live(host, prov, save)
    if left:
        raise Failed(f"{name}: cleanup failed for {', '.join(left)}")


def _step(host, r, step, prov, slot, save):
    key = f"{r['repo']}:{step}"
    if key in prov["done"]:
        return
    result = host.run(r["repo"], step, r[f"{step}_cmd"], slot)
    if not result["ok"]:
        raise Failed(f"{r['repo']}: {step} failed - {result['detail']}")
    prov["done"].append(key)
    save()


def _release(host, prov, save, what):
    left = _stop_live(host, prov, save)
    if left:
        raise Failed(f"cleanup of {what} failed for {', '.join(left)}")


def _stop_live(host, prov, save):
    """Stop every recorded temporary resource; return the ids not confirmed stopped."""
    left = []
    for receipt in list(prov["live"]):
        try:
            result = host.stop(receipt)
        except Exception as e:  # noqa: BLE001 — a transport failure is a retryable cleanup failure
            result = dict(state="failed", detail=str(e))
        entry = dict(id=receipt["id"], state=result["state"], detail=result["detail"])
        prov["cleanup"] = [c for c in prov["cleanup"] if c["id"] != entry["id"]] + [entry]
        if result["state"] == "removed":
            prov["live"].remove(receipt)
        else:
            left.append(receipt["id"])
    save()
    return left


class Verification:
    """`main.Deps.verification`: provisions in the slot the dispatcher grants."""

    def __init__(self, control):
        self.control, self.running = control, set()

    def pending(self):
        return [op for op in self.control.operations()
                if self.control.status(op).get("provisioning", {}).get("state") == "waiting_for_capacity"
                or (self.control.has_live(op) and op not in self.running)]

    def start(self, operation_id, slot):
        self.running.add(operation_id)
        try:
            self.control.provision(operation_id, slot)
        finally:
            self.running.discard(operation_id)

    def is_running(self, operation_id):
        return operation_id in self.running or self.control.has_live(operation_id)
