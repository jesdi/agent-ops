"""Deterministic, mutation-free gh reads feeding a triage session: the
issues in the cursor window (with truncated comments), the label
inventory, best-effort issue types, and the full open-issue list for
in-context duplicate detection."""
from __future__ import annotations

import json
import subprocess

MAX_COMMENTS = 20
MAX_COMMENT_CHARS = 1000
GH_TIMEOUT = 120

# Context budget for one session, measured on the serialization that actually
# reaches the prompt (_size). Per-comment caps alone leave the blob unbounded:
# uncapped bodies, up to 100 issues per window, 500 number+title pairs. Two
# things need this bound. Cost and attention — an unbounded blob drowns the
# session. And execve: `claude -p "$(cat …)"` still passes the prompt as ONE
# argument inside the container, and Linux caps a single argv element at
# MAX_ARG_STRLEN = 128 KiB. The prompt file (containers.triage_cmd) keeps the
# dispatcher's own podman argv small; this budget is what keeps the container's
# argv under the ceiling, with room for the prompt template around the blob.
MAX_BODY_CHARS = 4000
MIN_BODY_CHARS = 500
MAX_TITLE_CHARS = 120
MAX_LABELS = 150
MAX_OPEN_ISSUES = 300
# 128 KiB (ARGV_CEILING_BYTES) is the hard ceiling; the prompt template around
# the blob is ~2.5 KB, so 80 KB leaves ~48 KB of headroom for it and for future
# growth on either side, while still leaving the 100-issue worst case ~440
# chars of body each.
MAX_CONTEXT_BYTES = 80_000
ARGV_CEILING_BYTES = 128 * 1024  # MAX_ARG_STRLEN, for the tests to assert on
TRUNC_MARK = "\n… [truncated]"


class PrefetchError(Exception):
    pass


class ContextTooLargeError(PrefetchError):
    """Even a fully stripped window does not fit the budget. Raised rather
    than returned so the sweep fails this repo loudly (a FAILED line, cursor
    untouched) instead of handing the container an argv it cannot exec."""


def _gh(run, args: list[str]) -> str:
    out = run(["gh"] + args, capture_output=True, text=True,
              timeout=GH_TIMEOUT)
    if out.returncode != 0:
        raise PrefetchError(f"gh {' '.join(args[:3])} failed: {out.stderr}")
    return out.stdout


def _json(run, args: list[str]):
    try:
        return json.loads(_gh(run, args))
    except json.JSONDecodeError as e:
        raise PrefetchError(f"gh {' '.join(args[:3])}: bad JSON: {e}") from e


def _comments(run, repo: str, number: int) -> list[dict]:
    data = _json(run, ["issue", "view", str(number), "--repo", repo,
                       "--json", "comments"])
    out = []
    for c in (data.get("comments") or [])[-MAX_COMMENTS:]:
        out.append({
            "author": (c.get("author") or {}).get("login", ""),
            "body": str(c.get("body", ""))[:MAX_COMMENT_CHARS],
        })
    return out


def _truncate_bodies(issues: list[dict], limit: int) -> list[str]:
    notes = []
    for i in issues:
        body = str(i.get("body") or "")
        if len(body) > limit:
            i["body"] = body[:limit] + TRUNC_MARK
            notes.append(f"#{i.get('number', '?')}: body truncated to "
                         f"{limit} chars")
    return notes


def _set_note(notes: list[str], prefix: str, text: str) -> None:
    """Replace any earlier note with this prefix — a later, harder shed of the
    same field supersedes it, and two contradictory notes would be worse than
    none."""
    notes[:] = [n for n in notes if not n.startswith(prefix)]
    notes.append(text)


def _size(blob: dict) -> int:
    # indent=2, exactly as triage._run_session serializes it into the prompt.
    # Measuring the compact form instead understates by ~25%, which is how a
    # "bounded" blob previously reached 131 KB in the prompt — past the ceiling
    # on the `claude -p "$(cat …)"` argument.
    return len(json.dumps(blob, indent=2))


def bound(blob: dict) -> dict:
    """A copy of the blob trimmed to the context budget, carrying a
    `truncated` list that names every reduction so the session knows its
    context is incomplete (and can say so rather than, say, ruling out a
    duplicate it was never shown).

    Sheds in increasing order of value, and every step is conditional on the
    blob still being over budget: the open-issue list, then comments, then
    bodies to a floor, then label descriptions, then titles, then the label
    inventory, and finally bodies to whatever per-issue allowance is left. The
    last step is what makes the budget *binding* — the earlier fixed caps can
    all be applied and still leave a 100-issue window with a large label
    inventory over the ceiling, which is #1's failure mode one threshold
    higher. If even an empty-bodied window will not fit,
    ContextTooLargeError says so rather than letting the container's execve
    fail.

    Issues themselves are never dropped: a successful sweep advances the cursor
    past this window, so a dropped issue would never be triaged again."""
    out = json.loads(json.dumps(blob))  # never mutate the caller's blob
    issues = out.get("issues") or []
    # Attached up front, and only ever mutated in place, so the notes count
    # against the budget like everything else — a long shed list is ~400 bytes.
    notes: list[str] = out.setdefault("truncated", [])
    notes.clear()
    notes.extend(_truncate_bodies(issues, MAX_BODY_CHARS))
    open_issues = out.get("open_issues") or []
    if len(open_issues) > MAX_OPEN_ISSUES:
        notes.append(f"open_issues: {len(open_issues)} open, showing the "
                     f"first {MAX_OPEN_ISSUES} — duplicate detection is "
                     f"incomplete")
        out["open_issues"] = open_issues[:MAX_OPEN_ISSUES]
    if _size(out) > MAX_CONTEXT_BYTES and out.get("open_issues"):
        notes.append(f"open_issues: all {len(out['open_issues'])} dropped to "
                     f"fit the context budget — no duplicate detection")
        out["open_issues"] = []
    if _size(out) > MAX_CONTEXT_BYTES and any(i.get("comments") for i in issues):
        notes.append("comments: dropped from every issue to fit the context "
                     "budget")
        for i in issues:
            i["comments"] = []
    if _size(out) > MAX_CONTEXT_BYTES:
        _truncate_bodies(issues, MIN_BODY_CHARS)
        notes[:] = [n for n in notes if "body truncated" not in n]
        _set_note(notes, "bodies:",
                  f"bodies: truncated to {MIN_BODY_CHARS} chars to fit the "
                  f"context budget")
    labels = out.get("labels") or []
    if _size(out) > MAX_CONTEXT_BYTES and any(l.get("description")
                                              for l in labels):
        notes.append("labels: descriptions dropped to fit the context budget "
                     "— names only")
        for l in labels:
            l["description"] = ""
    if _size(out) > MAX_CONTEXT_BYTES and any(
            len(str(i.get("title") or "")) > MAX_TITLE_CHARS for i in issues):
        notes.append(f"titles: truncated to {MAX_TITLE_CHARS} chars to fit "
                     f"the context budget")
        for i in issues:
            i["title"] = str(i.get("title") or "")[:MAX_TITLE_CHARS]
    if _size(out) > MAX_CONTEXT_BYTES and len(labels) > MAX_LABELS:
        notes.append(f"labels: inventory of {len(labels)} shown as the first "
                     f"{MAX_LABELS} — a label you cannot see here still "
                     f"exists, so add none you were not shown")
        out["labels"] = labels[:MAX_LABELS]
    if _size(out) > MAX_CONTEXT_BYTES and issues:
        # Whatever is left goes to bodies, divided evenly. Halve on each miss:
        # JSON escaping makes the cost of a body more than its length, so solve
        # it by measurement rather than arithmetic. Terminates at 0.
        stripped = json.loads(json.dumps(out))
        for i in stripped["issues"]:
            i["body"] = ""
        marker_cost = len(json.dumps(TRUNC_MARK))  # escaped, per truncation
        allowance = max(0, (MAX_CONTEXT_BYTES - _size(stripped)) // len(issues)
                        - marker_cost)
        _truncate_bodies(issues, allowance)
        while _size(out) > MAX_CONTEXT_BYTES and allowance > 0:
            allowance //= 2
            _truncate_bodies(issues, allowance)
        _set_note(notes, "bodies:",
                  f"bodies: truncated to {allowance} chars to fit the context "
                  f"budget")
    if _size(out) > MAX_CONTEXT_BYTES:
        raise ContextTooLargeError(
            f"triage context is {_size(out)} bytes with everything shed, over "
            f"the {MAX_CONTEXT_BYTES}-byte budget: {len(issues)} issues, "
            f"{len(out.get('labels') or [])} labels")
    return out


def prefetch(repo: str, cursor: str, run=subprocess.run) -> dict:
    window = _json(run, [
        "issue", "list", "--repo", repo, "--state", "open",
        "--search", f"updated:>{cursor}",
        "--json", "number,title,body,labels,author", "--limit", "100"])
    issues = [{
        "number": i["number"],
        "title": i.get("title", ""),
        "body": i.get("body", ""),
        "author": (i.get("author") or {}).get("login", ""),
        "labels": [l["name"] for l in i.get("labels", [])],
        "comments": _comments(run, repo, i["number"]),
    } for i in window]
    blob = {"repo": repo, "cursor": cursor, "issues": issues,
            "labels": [], "issue_types": [], "open_issues": []}
    if not issues:
        return blob  # skip inventory fetches: caller spawns no session

    blob["labels"] = [
        {"name": l["name"], "description": l.get("description", "")}
        for l in _json(run, ["label", "list", "--repo", repo,
                             "--json", "name,description", "--limit", "100"])]
    # Issue types exist only for org-owned repos; degrade silently.
    owner = repo.split("/")[0]
    out = run(["gh", "api", f"orgs/{owner}/issue-types"],
              capture_output=True, text=True, timeout=GH_TIMEOUT)
    if out.returncode == 0:
        try:
            blob["issue_types"] = json.loads(out.stdout)
        except json.JSONDecodeError:
            pass
    blob["open_issues"] = _json(run, [
        "issue", "list", "--repo", repo, "--state", "open",
        "--limit", "500", "--json", "number,title"])
    return blob
