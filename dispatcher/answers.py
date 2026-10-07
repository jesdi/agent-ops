"""The answers protocol: what the review page's selections become on disk.

The console posts the answers of an open operator request as an `answers`
intent; the dispatcher drains it into the request's answers file in the
worktree, and a submission wakes the session to read that file. This module
owns the file (name, shape, read, write), the submit vocabulary, the
question-id grammar, and the drain's rules: which intent of a pass wins, and
why one is dropped."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Callable, Iterable, TypedDict

from dispatcher import intents
from dispatcher.state import (PARK_HUMAN, PARK_REVIEW, Stage, TaskState,
                              read_regular)
from dispatcher.workspace import remove_worktree_file, write_worktree_file

# The answers file of each operator request kind, and the stage it answers.
ANSWERS_FILES = {"plan-approval": ("review-answers.json", "plan"),
                 "answers": ("questionnaire-answers.json", "spec")}
# A submission, and the message that wakes the session with it.
SUBMITS = ("changes", "approve")
WAKE = {"changes": "Apply them as feedback.", "approve": "Approved."}
# The id of a question block (`data-q`) and of its answer key.
QUESTION_ID = r"[a-z0-9_-]{1,64}"
# Twice the console's 64 KiB body cap, plus room for the envelope.
ANSWERS_FILE_MAX_BYTES = 256 * 1024


class AnswersDoc(TypedDict, total=False):
    """The answers file. Read back, every field may be missing or of
    another type: a session writes the file too (a text answer)."""
    v: int
    stage: str
    submitted: str | None       # one of SUBMITS; None = a draft
    submitted_at: str | None
    actor: str                  # "text" when a session wrote it
    revision: str | None        # the request revision; None from a session
    answers: dict


def read(worktree: str | Path, kind: str) -> AnswersDoc | None:
    """The answers file of a request kind; None when it is missing, not a
    regular file of a sane size, or not a JSON object."""
    name, _ = ANSWERS_FILES[kind]
    raw = read_regular(Path(worktree) / ".agent" / name, ANSWERS_FILE_MAX_BYTES)
    try:
        doc = json.loads(raw) if raw is not None else None
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def write(worktree: str | Path, kind: str, doc: AnswersDoc) -> None:
    """Write the answers file whole, never through a symlink, and only into
    an `.agent/` the session made (write_worktree_file would create it)."""
    if not stat.S_ISDIR(os.lstat(Path(worktree) / ".agent").st_mode):
        raise OSError(f"{worktree}/.agent is not a directory")
    name, _ = ANSWERS_FILES[kind]
    write_worktree_file(worktree, ".agent", name, json.dumps(doc, indent=2))


def discard_stale(worktree: str | Path, kind: str, revision: str) -> None:
    """A request of `kind` is armed at `revision`: remove the answers file
    unless it is a draft made on that revision. A submission was read by
    the session it woke, and one a session wrote from a text answer names
    no revision: either would block the drafts of the new round."""
    doc = read(worktree, kind)
    if doc and doc.get("submitted") is None and doc.get("revision") == revision:
        return
    name, _ = ANSWERS_FILES[kind]
    try:
        remove_worktree_file(worktree, ".agent", name)
    except OSError as exc:   # `.agent` is no real directory: nothing to remove
        print(f"[warn] {name} not removed: {exc}", file=sys.stderr)


def _valid(intent: intents.Intent, revision: str | None) -> bool:
    p = intent.payload
    return (p.get("submit") in (None, *SUBMITS)
            and (revision is None or (bool(revision) and p.get("revision") == revision)))


def select(pending: Iterable[intents.Intent],
           revision: str | None) -> intents.Intent | None:
    """The answers intent of one task that wins: the newest with a valid
    submit made on `revision`, else the newest valid draft, else None. On a
    tie the later one in `pending` wins. `revision` None takes any revision
    (the console's restore, whose request revision a pending intent may
    predate)."""
    valid = [i for i in pending if _valid(i, revision)]
    return max(reversed(valid), default=None, key=lambda i: (
        i.payload.get("submit") is not None, i.created_at, i.path.name))


def waits_for_operator(task: TaskState) -> bool:
    """Parked for input or review, or at the gate before the grace park."""
    return (task.park in (PARK_HUMAN, PARK_REVIEW)
            or (not task.park and task.stage is Stage.AWAITING_PLAN_REVIEW))


def disk_revision(task: TaskState | None) -> str:
    """The revision of the task's open request as it is on disk now; ""
    when there is no request or the revision cannot be told."""
    # Imported here: artifacts reads QUESTION_ID from this module.
    from dispatcher.artifacts import page_revision, plan_revision
    req = task.operator_request if task is not None else None
    if req is None:
        return ""
    if req.kind == "plan-approval":
        return plan_revision(task.worktree, req.path, task.spec_path).fingerprint
    return page_revision(Path(task.worktree) / req.path)[0]


def request_reason(task: TaskState | None, intent: intents.Intent,
                   on_disk: str | None = None) -> str | None:
    """Why the intent does not answer the task's open request; None when it
    does. The request's stored revision can be one pass old (the session
    rewrote the page since), so the revision on disk must match too.
    `on_disk` is read when not given."""
    submit = intent.payload.get("submit")
    if submit not in (None, *SUBMITS):
        return f"unknown submit {submit!r}"
    if task is None or task.operator_request is None:
        return "no open request"
    revision = task.operator_request.fingerprint
    if not _valid(intent, revision):
        return "stale revision"
    # ponytail: a page that cannot be read now ("") is no evidence of a new
    # revision; the session's next report settles the request.
    if (on_disk if on_disk is not None else disk_revision(task)) not in ("", revision):
        return "stale revision"
    return None


def _submitted(task: TaskState, revision: object) -> bool:
    """The file on disk is a submission a draft must not overwrite: one made
    on this revision, or one with no revision (a session wrote it from a
    text answer). Garbage on disk is no submission."""
    doc = read(task.worktree, task.operator_request.kind) or {}
    return (doc.get("submitted") is not None
            and doc.get("revision") in (None, revision))


def drop_reason(task: TaskState | None, intent: intents.Intent) -> str | None:
    """Why an answers intent must not be applied; None when it may be. A
    draft against a submitted file is stale whatever the task does now, so
    that reason comes before "session busy" (the submission's own wake
    makes the task busy)."""
    reason = request_reason(task, intent)
    if reason:
        return reason
    assert task is not None
    if (intent.payload.get("submit") is None
            and _submitted(task, intent.payload.get("revision"))):
        return "already submitted"
    if not waits_for_operator(task):
        return "session busy"
    return None


def record(task: TaskState, intent: intents.Intent) -> str | None:
    """Write the intent as the answers file of the task's open request.
    Returns the message that wakes the session for a submission, None for a
    draft."""
    kind = task.operator_request.kind
    name, stage = ANSWERS_FILES[kind]
    submit = intent.payload.get("submit")
    write(task.worktree, kind, {
        "v": 1, "stage": stage, "submitted": submit,
        "submitted_at": intent.created_at if submit else None,
        "actor": intent.actor or "operator",
        "revision": intent.payload.get("revision"),
        "answers": intent.payload.get("answers")})
    return f"Answers in .agent/{name} ({submit}). {WAKE[submit]}" if submit else None


def without_superseded(pending: list[intents.Intent],
                       task_for: Callable[[intents.Intent], TaskState | None]
                       ) -> list[intents.Intent]:
    """Per task, only one answers intent of a pass that answers the open
    request is applied: the one `select` picks. The others that answer it
    are deleted unapplied; one that does not stays and drops with its
    reason (a stale tab never costs a current answer). A task file that
    does not load leaves its intents to the drain, which fails each."""
    groups: dict[tuple[str, int], list[intents.Intent]] = {}
    for i in pending:
        if i.action == "answers":
            groups.setdefault((i.target, i.issue), []).append(i)
    losers: dict[Path, intents.Intent] = {}
    for group in groups.values():
        try:
            task = task_for(group[0])
        except Exception:
            continue
        on_disk = disk_revision(task)
        competing = [i for i in group if request_reason(task, i, on_disk) is None]
        if not competing:
            continue
        win = select(competing, task.operator_request.fingerprint)
        for i in competing:
            if i is not win:
                print(f"[info] intent {i.path.name} superseded by {win.path.name}",
                      file=sys.stderr)
                losers[i.path] = i
    for i in losers.values():
        intents.delete_intent(i)
    return [i for i in pending if i.path not in losers]
