"""An exam in step with its pull request: what the PR's head, its merge or close, the
repo's `.pr-exam.json` and the owner's bypass labels make of the exam, and the `exam`
status its state earns.

These are the transitions on one exam's state document. `dispatcher.exams` reads GitHub,
holds the lock around them and runs the jobs. The config is read at the PR's base ref,
never its head. A label counts only when its most recent `labeled` event was made by
`operator_login`."""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

CONFIG = ".pr-exam.json"
STATE_FILE = "state.json"


class ExamState(StrEnum):
    NEW = "new"                 # accepted, GitHub not read yet
    GENERATING = "generating"   # a generation or delta session is queued or running
    OPEN = "open"               # questions are waiting for answers
    GRADING = "grading"         # a submission is being graded
    PASSED = "passed"           # passed on `head_sha`
    SKIPPED = "skipped"         # the owner's skip label is honoured
    DISABLED = "disabled"       # the repo's config turns exams off
    ERROR = "error"             # the last job or the config failed; `detail` says why


@dataclass(frozen=True)
class Bypass:
    """What the repo's config and the owner's labels make of a PR's exam."""
    state: ExamState | None = None   # the state they put the exam in; None lets the exam go on
    detail: str = ""             # why, for `ExamState.ERROR`
    deferred: bool = False       # the exam goes on as debt


def _config(io, repo: str, base_ref: str) -> dict:
    text = io.github.file(repo, base_ref, CONFIG)
    if text is None:
        return {"errors": [], "enabled": True, "labels": {}}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / CONFIG
        path.write_text(text)
        try:
            return io.pr_exam.config(path)
        except ValueError as exc:   # the skill refused the config: its output is the errors
            return {"errors": [str(exc)], "enabled": True, "labels": {}}


def _applied_by(events: list[dict], label: str) -> str | None:
    """Who last added `label`; None when it was never added."""
    actors = [e["actor"] for e in events if e["event"] == "labeled" and e["label"] == label]
    return actors[-1] if actors else None


def bypass(cfg, repo: str, n: int, pr: dict, io) -> Bypass:
    """The bypass of the PR as `pr` read it, by the config at its base ref and its labels."""
    conf = _config(io, repo, pr["base_ref"])
    if conf["errors"]:
        return Bypass(ExamState.ERROR, "; ".join(conf["errors"]))
    if not conf["enabled"]:
        return Bypass(ExamState.DISABLED)
    return _by_labels(cfg, conf["labels"], repo, n, pr, io)


def _by_labels(cfg, labels: dict, repo: str, n: int, pr: dict, io) -> Bypass:
    """The bypass by the PR's labels, `labels` mapping each bypass label to its mode."""
    marked = [label for label in pr["labels"] if label in labels]
    if not marked:
        return Bypass()
    if not cfg.operator_login:
        return Bypass(ExamState.ERROR,
                      "operator_login is not set in targets.yaml, so no bypass label can be honoured")
    return _by_owner(labels, marked, io.github.label_events(repo, n), cfg.operator_login)


def _by_owner(labels: dict, marked: list[str], events: list[dict], login: str) -> Bypass:
    """The bypass of the `marked` labels that `login` added last."""
    modes = {labels[label] for label in marked if _applied_by(events, label) == login}
    # ponytail: skip outranks defer when the owner added both labels
    return Bypass(ExamState.SKIPPED) if "skip" in modes else Bypass(deferred="defer" in modes)


def busy(cur: dict) -> bool:
    """A job or a submission holds the exam: only the job's result may move it."""
    return bool(cur["job"] or cur["grading"])


def restart(cur: dict, d: Path) -> None:
    """Drop every attempt and everything kept for them; the exam is generated afresh. The
    last status posted stays: it is what GitHub shows, and what tells a restarted exam
    from one that never started."""
    for path in d.iterdir():
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.name != STATE_FILE:
            path.unlink(missing_ok=True)
    cur.update(state=ExamState.GENERATING, head_sha=None, attempt=0, results=[], passed_concepts=[],
               grading=None, followup=None, delta=False, detail="", job=None)


def new_head(cur: dict, d: Path) -> None:
    """The PR's head moved under the exam. A pass is kept: a delta judgment finds what the
    new head adds. Anything else starts over on the new head."""
    if cur["state"] == ExamState.PASSED or cur["delta"]:
        cur.update(state=ExamState.GENERATING, delta=True)
    else:
        restart(cur, d)


_SUCCESS = {ExamState.PASSED: "Exam passed", ExamState.SKIPPED: "bypassed"}
_DEFERRED = (ExamState.OPEN, ExamState.GRADING)   # a deferral covers the head once its questions are out


def _success(cur: dict) -> str:
    """How the exam's state earns success; "" while it earns none."""
    deferred = cur["debt"] and cur["state"] in _DEFERRED
    return _SUCCESS.get(cur["state"]) or ("deferred" if deferred else "")


def status(cur: dict, sha: str) -> dict | None:
    """The `exam` status the exam's state earns on `sha`: the one rule behind every status
    queued. None for a merged PR, whose exam is only debt, and for a disabled repo."""
    if cur["merged"] or cur["state"] == ExamState.DISABLED:
        return None
    success = _success(cur)
    return {"sha": sha, "state": "success" if success else "pending",
            "description": success or "Exam in progress"}


def _untouched(cur: dict) -> bool:
    """Never started: no attempt, no debt, no status and no job."""
    return cur["attempt"] == 0 and not cur["debt"] and cur["status"] is None and not cur["job"]


def _ended(cur: dict, pr: dict, bypass: Bypass) -> bool:
    """Whether the PR's end ends the exam with it: closed unmerged, or merged under a skip,
    in a disabled repo or before any exam started."""
    if not pr["merged"]:
        return pr["state"] == "closed"
    return bypass.state in (ExamState.SKIPPED, ExamState.DISABLED) or _untouched(cur)


def _replaced(cur: dict, pr: dict, bypass: Bypass) -> bool:
    """Whether the PR's head left the exam's behind. A deferred exam keeps the questions it has out."""
    if cur["state"] not in (ExamState.OPEN, ExamState.PASSED) or pr["head_sha"] == cur["head_sha"]:
        return False
    return not (bypass.deferred and cur["state"] == ExamState.OPEN)


def _follow(cur: dict, d: Path, pr: dict, bypass: Bypass) -> None:
    """Move an exam that nothing holds to where its PR and its bypass put it."""
    if bypass.state == ExamState.ERROR:
        cur.update(state=ExamState.ERROR, detail=bypass.detail)
    elif bypass.state:   # skipped or disabled: no attempt is kept
        restart(cur, d)
        cur.update(state=bypass.state, head_sha=pr["head_sha"])
    elif cur["state"] in (ExamState.SKIPPED, ExamState.DISABLED):   # the bypass is gone: examined after all
        cur["state"] = ExamState.NEW
    elif _replaced(cur, pr, bypass):
        new_head(cur, d)


def over(cur: dict) -> bool:
    """A merged PR's exam is over once it is passed on the merged head."""
    return cur["merged"] and cur["state"] == ExamState.PASSED


def reconcile(cur: dict, d: Path, pr: dict, bypass: Bypass) -> bool:
    """Bring the exam in line with its PR as `pr` read it and `bypass` decided; True when
    that ends the exam, which the caller deletes (its learning records stay).

    A PR closed unmerged ends its exam. A merged PR ends an exam that is passed on the
    merged head, skipped or never started; any other exam of a merged PR is debt until it
    is passed. An honoured defer is debt too, and holds only while the label is there."""
    if _ended(cur, pr, bypass):
        return True
    cur.update(merged=pr["merged"], title=pr["title"])
    if not busy(cur):
        _follow(cur, d, pr, bypass)
    cur["debt"] = (bypass.deferred or pr["merged"]) and cur["state"] != ExamState.PASSED
    cur["due"] = status(cur, pr["head_sha"])
    return over(cur)
