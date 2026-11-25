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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, NamedTuple, TypedDict, get_args

from dispatcher import intents
from dispatcher.state import TaskState, operator_request, read_regular
from dispatcher.status import Kind
from dispatcher.workspace import remove_worktree_file, write_worktree_file

# The answers file of each operator request kind, and the stage it answers.
ANSWERS_FILES = {"plan-approval": ("review-answers.json", "plan"),
                 "answers": ("questionnaire-answers.json", "spec")}
# A submission, and the message that wakes the session with it.
Submit = Literal["changes", "approve"]
SUBMITS: tuple[Submit, ...] = get_args(Submit)
WAKE = {"changes": "Apply them as feedback.", "approve": "Approved."}
# The id of a question block (`data-q`) and of its answer key.
QUESTION_ID = r"[a-z0-9_-]{1,64}"
# Twice the console's 64 KiB body cap, plus room for the envelope.
ANSWERS_FILE_MAX_BYTES = 256 * 1024


# Why an answers intent is dropped (request_reason, drop_reason), besides an
# unknown submit, which no submission has.
DropReason = Literal["no open request", "stale revision", "already submitted",
                     "session busy"]


@dataclass(frozen=True)
class Submission:
    """An answers intent that submits (not a draft): what its drop event
    records, and what the console reads back as a delivery."""
    submit: Submit
    revision: str | None
    created_at: str

    @classmethod
    def of(cls, intent: intents.Intent) -> Submission | None:
        """The intent's submission; None for a draft or another action."""
        p = intent.payload if isinstance(intent.payload, dict) else {}
        if intent.action != "answers" or not _submits(p.get("submit"), p.get("revision")):
            return None
        return cls(p["submit"], p.get("revision"), str(intent.created_at))

    def to_event(self) -> dict:
        return asdict(self)

    @classmethod
    def from_event(cls, raw: object) -> Submission | None:
        """The submission of an event line; None when the line holds none
        or a hand-edited one of the wrong shape."""
        raw = raw if isinstance(raw, dict) else {}
        at = raw.get("created_at")
        if not (_submits(raw.get("submit"), raw.get("revision")) and isinstance(at, str)):
            return None
        return cls(raw["submit"], raw.get("revision"), at)


def _submits(submit: object, revision: object) -> bool:
    """A submit kind, on a revision or none (a session wrote the file)."""
    return submit in SUBMITS and isinstance(revision, (str, type(None)))


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


def approval(task: TaskState, intent: intents.Intent) -> str:
    """The plan revision the task records as approved once the drained
    submission `intent` is applied: its revision for an `approve` of a
    plan-approval request, "" for a change request. A submission to another
    kind of request leaves the record as it is. The answers file carries the
    selections to the session, never the approval: the session writes it."""
    if operator_request(task).kind != "plan-approval":
        return task.plan_approval
    submit = intent.payload.get("submit")
    return intent.payload.get("revision", "") if submit == "approve" else ""


def _valid(intent: intents.Intent, revision: str) -> bool:
    p = intent.payload
    return (p.get("submit") in (None, *SUBMITS)
            and bool(revision) and p.get("revision") == revision)


def select(pending: Iterable[intents.Intent],
           revision: str) -> intents.Intent | None:
    """The answers intent of one task that wins: the newest with a valid
    submit made on `revision`, else the newest valid draft, else None. On a
    tie the later one in `pending` wins."""
    valid = [i for i in pending if _valid(i, revision)]
    return max(reversed(valid), default=None, key=lambda i: (
        i.payload.get("submit") is not None, i.created_at, i.path.name))


_WAITS_FOR_OPERATOR = frozenset({Kind.PARKED_HUMAN, Kind.PARKED_ANSWERS, Kind.PARKED_ACCESS,
                                 Kind.PARKED_REVIEW, Kind.HELD_PLAN_APPROVAL})


def waits_for_operator(task: TaskState) -> bool:
    """Parked for input or review, or held at the gate before the grace park."""
    return task.status.kind in _WAITS_FOR_OPERATOR


_LIVE = frozenset({Kind.RUNNING, Kind.WAKING, Kind.HELD_LOGIN})


def held(intent: intents.Intent,
         task_for: Callable[[intents.Intent], TaskState | None]) -> bool:
    """A submission made while the session is live: it stays pending until
    the task waits for the operator again, then drains as any other."""
    if Submission.of(intent) is None:
        return False
    task = task_for(intent)
    return task is not None and task.status.kind in _LIVE


def disk_revision(task: TaskState | None) -> str:
    """The revision of the task's open request as it is on disk now; ""
    when there is no request or the revision cannot be told."""
    # Imported here: artifacts reads QUESTION_ID from this module.
    from dispatcher.artifacts import page_revision, plan_revision
    req = operator_request(task) if task is not None else None
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
    request = operator_request(task) if task is not None else None
    if request is None:
        return "no open request"
    return _revision_reason(task, intent, request.fingerprint, on_disk)


def _revision_reason(task: TaskState, intent: intents.Intent, revision: str,
                     on_disk: str | None) -> str | None:
    """"stale revision" unless the intent was made on the request's
    revision and the page on disk is still that revision; None when it is."""
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
    doc = read(task.worktree, operator_request(task).kind) or {}
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
    kind = operator_request(task).kind
    name, stage = ANSWERS_FILES[kind]
    submit = intent.payload.get("submit")
    write(task.worktree, kind, {
        "v": 1, "stage": stage, "submitted": submit,
        "submitted_at": intent.created_at if submit else None,
        "actor": intent.actor or "operator",
        "revision": intent.payload.get("revision"),
        "answers": intent.payload.get("answers")})
    return f"Answers in .agent/{name} ({submit}). {WAKE[submit]}" if submit else None


def _superseded(group: list[intents.Intent],
                task_for: Callable[[intents.Intent], TaskState | None]
                ) -> list[intents.Intent]:
    """The intents of one task's group that answer its open request and
    lose to the one `select` picks. A task file that does not load leaves
    the group to the drain, which fails each intent."""
    try:
        task = task_for(group[0])
    except Exception:
        return []
    competing = _answering(task, group)
    if not competing:
        return []
    return _losers(competing, select(competing, operator_request(task).fingerprint))


def _answering(task: TaskState | None, group: list[intents.Intent]
               ) -> list[intents.Intent]:
    """The intents of the group that answer the task's open request."""
    on_disk = disk_revision(task)
    return [i for i in group if request_reason(task, i, on_disk) is None]


def _losers(competing: list[intents.Intent], win: intents.Intent
            ) -> list[intents.Intent]:
    """The competing intents other than `win`, each logged as superseded."""
    losers = [i for i in competing if i is not win]
    for i in losers:
        print(f"[info] intent {i.path.name} superseded by {win.path.name}",
              file=sys.stderr)
    return losers


def without_superseded(pending: list[intents.Intent],
                       task_for: Callable[[intents.Intent], TaskState | None]
                       ) -> list[intents.Intent]:
    """Per task, only one answers intent of a pass that answers the open
    request is applied: the one `select` picks. The others that answer it
    are deleted unapplied; one that does not stays and drops with its
    reason (a stale tab never costs a current answer)."""
    groups: dict[tuple[str, int], list[intents.Intent]] = {}
    for i in pending:
        if i.action == "answers":
            groups.setdefault((i.repository, i.issue), []).append(i)
    losers = {i.path for g in groups.values() for i in _superseded(g, task_for)}
    for i in pending:
        if i.path in losers:
            intents.delete_intent(i)
    return [i for i in pending if i.path not in losers]


@dataclass(frozen=True)
class Delivery:
    """What became of the task's latest submission."""
    state: Literal["queued", "delivered", "dropped"]
    reason: DropReason | Literal[""]    # "" unless dropped
    submit: Submit
    revision: str | None                # None: a session wrote the answers file


class _Outcome(NamedTuple):
    """A submission's outcome, ordered by submission time; on a tie the
    answers file (delivered) wins over a drop."""
    at: str
    delivered: bool
    delivery: Delivery


def delivery(task: TaskState, pending: Iterable[intents.Intent],
             events: Iterable[dict]) -> Delivery | None:
    """A pending submission of the task is queued; otherwise the newer of
    the answers files' submission and the latest drop of one decides.
    None when the task has no submission. Drafts never count."""
    queued = _queued(task, pending)
    if queued:
        return queued
    latest = max(_delivered(task) + _dropped(task, events), default=None,
                 key=lambda o: o[:2])
    return latest.delivery if latest else None


def _queued(task: TaskState, pending: Iterable[intents.Intent]) -> Delivery | None:
    """The task's latest pending submission made on a revision."""
    queued = [s for i in pending if (i.repository, i.issue) == (task.repository, task.issue)
              and (s := Submission.of(i)) is not None and s.revision is not None]
    s = max(queued, default=None, key=lambda s: s.created_at)
    return Delivery("queued", "", s.submit, s.revision) if s else None


def _delivered(task: TaskState) -> list[_Outcome]:
    """The submission of each answers file of the task (both kinds can exist)."""
    out = []
    for kind in ANSWERS_FILES:
        doc = read(task.worktree, kind) or {}
        submit, revision = doc.get("submitted"), doc.get("revision")
        if _submits(submit, revision):
            out.append(_Outcome(str(doc.get("submitted_at") or ""), True,
                                Delivery("delivered", "", submit, revision)))
    return out


def _dropped(task: TaskState, events: Iterable[dict]) -> list[_Outcome]:
    """The latest drop of a submission of the task; none when that event
    line was hand-edited out of shape."""
    e = next((e for e in reversed(list(events)) if _is_drop(task, e)), {})
    s, reason = Submission.from_event(e.get("submission")), e.get("detail")
    if s is None or reason not in get_args(DropReason):
        return []
    return [_Outcome(s.created_at, False, Delivery("dropped", reason, s.submit, s.revision))]


def _is_drop(task: TaskState, e: dict) -> bool:
    """e records the drop of a submission of the task."""
    return (e.get("event"), e.get("repository"), e.get("issue")) == (
        "intent-dropped", task.repository, task.issue) and "submission" in e
