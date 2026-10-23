"""Repository and board authorization and required-write verification for a confirmed operation.

The injectable `verifier` (a distinct execution identity, never the task's) offers:
  authorize(target) -> {"state": "granted" | "awaiting_admin" | "unsupported", "action": str}
  write(target, kind) -> resource id   kind is "branch", "pr", or "board"; raises PermissionError when refused
  lookup(target, kind) -> resource id | None   optional: finds a resource whose write response was lost
  cleanup(target, receipt) -> {"state": "removed" | "closed" | "failed", "detail": str}
The intent to write is saved before the write and the receipt right after it, so a retry reuses a
receipt, adopts a lost write through `lookup` (without one it stops rather than duplicate), and
recreates a resource whose receipt was already cleaned. Cleanup touches only the receipts recorded
here; a cleanup that raises is recorded as failed and the others still run. A repository that already
passed keeps its result on retry.
"""

# Repository targets are owner/name; board targets are board:<verified GraphQL id>.
# The trusted verifier must create a temporary board resource and return its exact
# receipt, then remove it through cleanup, just as for repository probes.
KINDS = ("branch", "pr")


def verification_complete(record):
    """Passed writes require positive cleanup evidence for every recorded receipt."""
    cleaned = {c["id"] for c in record.get("cleanup", []) if c["state"] in ("removed", "closed")}
    return (record.get("state") == "passed" and bool(record.get("receipts"))
            and all(r["id"] in cleaned for r in record["receipts"]))


def _cleanup(verifier, repo, receipt):
    try:
        return {"id": receipt["id"], **verifier.cleanup(repo, receipt)}
    except Exception as e:  # noqa: BLE001 — a transport failure is a resumable cleanup failure
        return {"id": receipt["id"], "state": "failed", "detail": str(e)}


def _write(verifier, repo, kind, record, save):
    """Write one resource; True when its receipt is recorded, False when refused or unresolved."""
    if record.get("intent") == kind:  # a previous attempt may have created it before dying
        found = getattr(verifier, "lookup", None)
        if found is None:
            record["unresolved"] = kind
            return False
        rid = found(repo, kind)
    else:
        rid = None
        record["intent"] = kind
        save()
    try:
        rid = rid or verifier.write(repo, kind)
    except PermissionError:
        record["denied"].append(kind)
        record.pop("intent")
        return False
    record["cleanup"] = [c for c in record["cleanup"] if c["id"] != rid]
    record["receipts"].append({"kind": kind, "id": rid})
    record.pop("intent")
    save()
    return True


def verify_repo(verifier, repo, record, save):
    """Advance one repository's record; `save` persists it after every step."""
    return verify_target(verifier, repo, record, save, KINDS)


def verify_target(verifier, repo, record, save, kinds):
    """Verify required writes and resume outstanding cleanup independently."""
    record.setdefault("receipts", [])
    record["authorization"] = verifier.authorize(repo)
    if record["authorization"]["state"] != "granted":
        record["state"] = "pending"
        return save()
    record.setdefault("cleanup", [])
    if verification_complete(record):
        return
    if record.get("state") != "passed":
        _required_writes(verifier, repo, record, save, kinds)
    _remaining_cleanup(verifier, repo, record, save)


def _required_writes(verifier, repo, record, save, kinds):
    cleanup = record["cleanup"]
    cleaned = {c["id"] for c in cleanup if c["state"] in ("removed", "closed")}
    record["receipts"] = [r for r in record["receipts"] if r["id"] not in cleaned]  # gone: write again
    record["denied"] = []
    record.pop("unresolved", None)
    for kind in kinds:
        if not any(r["kind"] == kind for r in record["receipts"]) and not _write(verifier, repo, kind, record, save):
            break
    record["state"] = "failed" if record["denied"] or "unresolved" in record else "passed"


def _remaining_cleanup(verifier, repo, record, save):
    done = {c["id"]: c for c in record["cleanup"] if c["state"] in ("removed", "closed")}
    for receipt in record["receipts"]:
        if receipt["id"] in done:
            continue
        done[receipt["id"]] = _cleanup(verifier, repo, receipt)
        record["cleanup"] = list(done.values())
        save()
    record["cleanup"] = list(done.values())
    save()
