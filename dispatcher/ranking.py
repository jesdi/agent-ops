"""A project's board rank order, from its board items and its member
repositories' issues. Pure: the GitHub reads live in github.py.

The rules are the backlog skill's rank.py (0.1.8), which the dispatcher used
to run as a project's `rank_cmd`: score = Impact / Effort, the Boost band
first, `Blocked by: #n` edges, and rows ordered available, blocked, then in
progress, with closed and Done issues dropped. One deliberate difference:
issues are keyed by repository and number, so on a board holding several
repositories an issue joins its own repository's body, labels and blockers."""
from __future__ import annotations

import re
from operator import itemgetter

from dispatcher.board import Item, _unique

_BLOCKED_LINE = re.compile(r"^\s*Blocked by:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ISSUE_REF = re.compile(r"#(\d+)")


def blocked_by(body: str | None) -> list[int]:
    """Sorted, unique issue numbers from every `Blocked by: #a, #b` line."""
    return sorted({int(ref) for line in _BLOCKED_LINE.finditer(body or "")
                   for ref in _ISSUE_REF.findall(line.group(1))})


def _whole(value: float | None) -> int | None:
    """A board number as rank.py read it: truncated to a whole number."""
    return None if value is None else int(value)


_NOT_ISSUES = {"DRAFT_ISSUE", "PULL_REQUEST"}


def _join(item: Item, titled: dict[str, dict]) -> tuple | None:
    """((repository, number), title, url) of the issue `item` links to. An
    item whose issue the board token cannot see joins by title (GitHub syncs
    linked-item titles to issue titles); a title no issue or two issues
    carry, a draft or a pull request joins nothing. (rank.py title-joined
    drafts too, so a draft sharing an issue's title ranked that issue.)"""
    if item.number is not None:
        return (item.repository.lower(), item.number), item.title, item.url
    if item.kind in _NOT_ISSUES:
        return None
    issue = titled.get(item.title)
    if issue is None:
        return None
    return (issue["repository"].lower(), issue["number"]), issue["title"], issue["url"]


def _record(item: Item, key: tuple, title: str, url: str, issue: dict) -> dict:
    """An item with its issue's fields; no issue reads as open, bare."""
    return {"key": key, "number": key[1], "title": title, "url": url,
            "state": (issue.get("state") or "OPEN").upper(),
            "body": issue.get("body") or "", "status": item.status,
            "labels": list(issue.get("labels") or ()),
            "impact": _whole(item.impact), "effort": _whole(item.effort),
            "boost": int(item.boost or 0)}


def _merge(items: list[Item], issues: list[dict]) -> list[dict]:
    """One record per item that joins an issue."""
    keyed = {(i["repository"].lower(), i["number"]): i for i in issues}
    titled = _unique((i["title"], i) for i in issues)
    merged = []
    for item in items:
        joined = _join(item, titled)
        if joined is not None:
            merged.append(_record(item, *joined, keyed.get(joined[0], {})))
    return merged


def _score(issue: dict) -> float | None:
    impact, effort = issue["impact"], issue["effort"]
    return None if impact is None or not effort else impact / effort


def _sort_key(issue: dict) -> tuple:
    value = _score(issue)
    return (-issue["boost"],             # band first: higher boost above everything
            value is None,               # scored before unscored within a band
            -(value or 0.0),             # higher score first
            -(issue["impact"] or 0),     # tie-break: higher impact
            issue["number"])


def _done(issue: dict) -> bool:
    return issue["state"] == "CLOSED" or issue["status"] == "Done"


def _blocked(issue: dict, by_key: dict) -> bool:
    """A blocker counts only while it is on the board and not done; a
    reference to anything else is a dropped edge."""
    repo = issue["key"][0]
    return any(not _done(by_key[(repo, n)])
               for n in blocked_by(issue["body"]) if (repo, n) in by_key)


def _bucket(issue: dict, by_key: dict) -> str | None:
    if _done(issue):
        return None
    if issue["status"] == "In progress":
        return "in_progress"
    return "blocked" if _blocked(issue, by_key) else "available"


def rank(items: list[Item], issues: list[dict]) -> list[dict]:
    """Rows in board rank order: available issues by band and score, then
    blocked ones, then those in progress, each of the last two by number.
    `issues` are {repository, number, title, url, body, state, labels} with
    label names; a row is {number, title, url, status, labels, blocked,
    score, boost}."""
    merged = _merge(items, issues)
    by_key = {i["key"]: i for i in merged}
    buckets: dict[str | None, list[dict]] = {
        "available": [], "blocked": [], "in_progress": [], None: []}
    for issue in merged:
        buckets[_bucket(issue, by_key)].append(issue)
    by_number = itemgetter("number")
    return ([_row(i, False) for i in sorted(buckets["available"], key=_sort_key)]
            + [_row(i, True) for i in sorted(buckets["blocked"], key=by_number)]
            + [_row(i, False) for i in sorted(buckets["in_progress"], key=by_number)])


def _row(issue: dict, blocked: bool) -> dict:
    return {"number": issue["number"], "title": issue["title"], "url": issue["url"],
            "status": issue["status"], "labels": issue["labels"], "blocked": blocked,
            "score": _score(issue), "boost": issue["boost"]}
