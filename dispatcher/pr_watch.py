"""The PR and CI watch: each pass reads every watched task's PR and every
CI-parked task's run, and feeds what changed to the lifecycle table."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

from dispatcher import host_auth, loops, pr_poll
from dispatcher.config import Config, Repository
from dispatcher.deps import Deps, _now
from dispatcher.executor import _Turn, _apply
from dispatcher.lifecycle import (Attempt, Event, EventType, Feedback, PR_ATTENTION,
                                  Transition)
from dispatcher.stage_launch import _queue_message
from dispatcher.state import Stage, TaskState, load_all, save
from dispatcher.status import Kind, ended


def _wake_ci(cfg: Config, deps: Deps, repository: Repository) -> None:
    for task in load_all(cfg.state_dir):
        if task.repository == repository.name and task.status.kind is Kind.PARKED_CI:
            _check_ci_run(cfg, deps, repository, task)


def _ci_conclusion(deps: Deps, repository: Repository, task: TaskState) -> str:
    """The conclusion of the run a task is parked on; "" while it runs, or
    when GitHub could not say (asked again next pass)."""
    try:
        return deps.github.run_status(repository, task.status.run_id)
    except (subprocess.CalledProcessError, OSError, host_auth.HostWait) as exc:
        print(f"[warn] run_status failed for #{task.issue} run {task.status.run_id}: {exc}",
              file=sys.stderr)
        return ""


def _check_ci_run(cfg: Config, deps: Deps, repository: Repository,
                  task: TaskState) -> None:
    """Once its run concluded, a CI-parked task hears the conclusion and the
    table decides: wake it, or park it for the operator when a failed run
    spends the last round (the row counts it)."""
    conclusion = _ci_conclusion(deps, repository, task)
    if not conclusion:
        return
    run_id = task.status.run_id
    dec = None
    if conclusion != "success":
        dec = loops.evaluate(task, loops.FailedRun(f"run {run_id} {conclusion}"),
                             cfg.loop_caps)
    turn = _Turn(cfg, deps, repository, admit=None, order=None, dry_run=False)
    outcome = _apply(turn, task, Event(EventType.CI_FINISHED, Attempt(dec)))
    if isinstance(outcome, Transition) and outcome.status.kind is Kind.WAKING:
        _queue_message(cfg, task.repository, task.issue,
                       f"E2E run {run_id} concluded: {conclusion} — "
                       f"fetch logs with: gh run view {run_id} --log-failed", "dispatcher")


def _poll_pr(cfg: Config, deps: Deps, repository: Repository, task: TaskState
             ) -> "tuple[TaskState, pr_poll.PollResult] | None":
    """One pr-open task's PR, classified; None when it has no PR yet.
    Resolves and saves the PR number on first sight."""
    numbered = _with_pr_number(cfg, deps, repository, task)
    if numbered is None:
        return None
    return numbered, _classify_pr(deps, repository, numbered)


def _with_pr_number(cfg: Config, deps: Deps, repository: Repository,
                    task: TaskState) -> TaskState | None:
    """`task` with its PR number, resolved and saved on first sight; None
    while its branch has no PR."""
    if task.pr_number:
        return task
    n = deps.github.pr_number_for_branch(repository, task.branch)
    if not n:
        print(f"[warn] #{task.issue}: no PR found for branch "
              f"{task.branch}", file=sys.stderr)
        return None
    task = replace(task, pr_number=n, updated_at=_now())
    save(cfg.state_dir, task)
    return task


def _classify_pr(deps: Deps, repository: Repository, task: TaskState
                 ) -> "pr_poll.PollResult":
    """The PR's state; a parked task's quiet or conflicted PR is classified
    again with its CI statuses."""
    payload = deps.github.pr_view(repository, task.pr_number)
    login = deps.github.viewer_login()
    res = pr_poll.classify(payload, task.feedback_cursor, login,
                           check_cursor=task.check_cursor,
                           conflict_cursor=task.conflict_cursor)
    if res.kind in ("quiet", "conflict") and task.status.kind is Kind.PARKED_PR:
        statuses = deps.github.ci_statuses(
            repository, payload.get("headRefOid") or "",
            payload.get("headRefName") or "")
        res = pr_poll.classify(payload, task.feedback_cursor, login,
                               check_cursor=task.check_cursor,
                               conflict_cursor=task.conflict_cursor,
                               ci_statuses=statuses)
    return res


# The stages whose PR is watched: parked on it, or addressing its review.
_PR_STAGES = (Stage.PR_OPEN, Stage.ADDRESS_REVIEW)


def _poll_prs(cfg: Config, deps: Deps, repository: Repository,
              dry_run: bool = False) -> None:
    """Watch every pr-open task's PR independently of CI access and capacity.
    Reaction (address-review spawn) is a wake, gated in _drive_wakes."""
    for task in load_all(cfg.state_dir):
        if (task.repository != repository.name or task.stage not in _PR_STAGES
                or ended(task.status)):
            continue
        polled = _poll_pr_or_warn(cfg, deps, repository, task)
        if polled is not None:
            _react_to_pr(cfg, deps, repository, *polled, dry_run)


def _poll_pr_or_warn(cfg: Config, deps: Deps, repository: Repository, task: TaskState
                     ) -> "tuple[TaskState, pr_poll.PollResult] | None":
    try:
        return _poll_pr(cfg, deps, repository, task)
    except (subprocess.CalledProcessError, OSError, host_auth.HostWait) as exc:
        print(f"[warn] PR poll failed for #{task.issue}: {exc}",
              file=sys.stderr)
        return None


def _react_to_pr(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                 res: "pr_poll.PollResult", dry_run: bool) -> None:
    """A merge or a close ends the task through the table; a merge whose
    teardown raises commits nothing and is retried next pass (a merged PR
    still reads merged)."""
    if res.kind not in _PR_ENDS:
        _react_to_open_pr(cfg, deps, repository, task, res, dry_run)
        return
    turn = _Turn(cfg, deps, repository, admit=None, order=None, dry_run=dry_run)
    try:
        _apply(turn, task, Event(_PR_ENDS[res.kind]))
    except (subprocess.CalledProcessError,
            subprocess.TimeoutExpired, OSError, host_auth.HostWait) as exc:
        print(f"[warn] PR {res.kind} not settled for #{task.issue}: {exc}",
              file=sys.stderr)


_PR_ENDS = {"merged": EventType.PR_MERGED, "closed": EventType.PR_CLOSED}


# The PR poll's attention kinds, and the cursor a kind advances first, so the
# same red check or conflict never raises it twice (feedback's cursor moves
# at the launch).
_PR_EVENTS = {a.kind: event for event, a in PR_ATTENTION.items()}
_PR_CURSORS = {EventType.PR_CHECK_FAILED: "check_cursor", EventType.PR_CONFLICT: "conflict_cursor"}


def _react_to_open_pr(cfg: Config, deps: Deps, repository: Repository,
                      task: TaskState, res: "pr_poll.PollResult", dry_run: bool) -> None:
    """The table decides: only a task parked on its PR acts on it. Woken,
    the round already queued reads the PR afresh; parked otherwise, only
    merge/close still matter."""
    if res.kind not in _PR_EVENTS:
        return
    event = _PR_EVENTS[res.kind]
    cursor = _PR_CURSORS.get(event)
    task = replace(task, **({cursor: res.latest_ts} if cursor else {}))
    turn = _Turn(cfg, deps, repository, admit=None, order=None, dry_run=dry_run)
    _apply(turn, task, Event(event, _pr_detail(cfg, task, res)))


def _pr_detail(cfg: Config, task: TaskState, res: "pr_poll.PollResult"
               ) -> Feedback | Attempt:
    """Feedback spends no round; a red check or a conflict spends a ci round."""
    if res.kind == "feedback":
        return Feedback(f"PR #{task.pr_number}, newest feedback {res.latest_ts}")
    return Attempt(loops.evaluate(
        task, loops.PRAttention(f"{res.kind} on PR #{task.pr_number}"), cfg.loop_caps))
