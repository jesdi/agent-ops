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


class PrefetchError(Exception):
    pass


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
