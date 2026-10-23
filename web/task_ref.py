"""How a request names a task: `/api/task/{project}/{repository_id}/{issue}`,
where repository_id is the "owner/name" repository with "/" written "~" (the
spelling state.task_key uses), so it is one path segment. The old
`/api/task/{project}/{issue}` still answers; it names no repository, and a
route resolves it only while one task has that number."""
from __future__ import annotations

from typing import Callable, NamedTuple

from fastapi import Depends, HTTPException

QUALIFIED = "/api/task/{project}/{repository_id}/{issue:int}"
LEGACY = "/api/task/{project}/{issue}"


class TaskRef(NamedTuple):
    target: str
    issue: int
    repo: str = ""      # "" = the request named no repository


def task_ref(project: str, issue: int, repository_id: str = "") -> TaskRef:
    repo = repository_id.replace("~", "/")
    if repo and repo.count("/") != 1:
        raise HTTPException(404, f"bad repository id {repository_id!r}")
    return TaskRef(project, issue, repo)


def ambiguous(target: str, issue: int, repos: list[str]) -> HTTPException:
    named = ", ".join(f"{r or '(unproven repository)'}#{issue}" for r in repos)
    return HTTPException(
        409, f"issue {issue} of {target} is ambiguous; name its repository: {named}")


def task_routes(router, method: str, tail: str = "", **kw) -> Callable:
    """Register one handler on the qualified path and, out of the schema, on
    the old one."""
    def register(fn):
        getattr(router, method)(QUALIFIED + tail, **kw)(fn)
        return getattr(router, method)(
            LEGACY + tail, include_in_schema=False, **kw)(fn)
    return register


REF = Depends(task_ref)
