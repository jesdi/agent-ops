"""One dispatcher pass: candidates → capacity → budget → claim → drive.

Stateless per pass — the view is rebuilt every time from the board (via
candidates), state files, stage.json signals, and session liveness. Run by a
systemd timer; one invocation = one pass."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Iterator

from dispatcher.convergence import pass_lock
from dispatcher.config import (Config, Repository, apply_stored_models,
                               by_project, load_config, policy_for,
                               referenced_providers, routed_providers)
from dispatcher.usage import ProviderUsage, Verdict, admits, verdict_note
from dispatcher.usage_providers import ADAPTERS, fetch_all
from dispatcher import (answers, claims, eventlog, failures, github_app, host_auth, intents, labels, loops,
                        messages, pr_poll, priority, queue_ops, tmux_migration,
                        triage, onboarding_sessions)
from dispatcher.state import write_json_atomic
from dispatcher.deps import Admit, Deps, _access, _now
from dispatcher.board import BoardError
from dispatcher.github import Candidate, GitHubClient

log = logging.getLogger(__name__)
from dispatcher import migrate, task_artifacts
from dispatcher import base_line
from dispatcher.lifecycle import (Attempt, Feedback, Event, EventType,
                                  Ignore, Reject, PR_ATTENTION, Ranked, SweepTombstone,
                                  Transition, decide, OPERATOR_INTENTS, operator_event)
from dispatcher.models import (Order, override_refusal,
                               track_from_labels)
from dispatcher.sessions import Sessions
from dispatcher.state import (NO_SLOT, UNREADABLE_TASK_FILE, WAKE_BLOCKED_PREFIX, Stage, TaskState, allocate_slot,
                              load,
                              load_all, max_slots,
                              next_stage, end, save, _clear_wake_blocked, _rank_rejected, _rank_rejected_path,
                              _readable_tasks, _wake_blocked_path)
from dispatcher.status import (_REPLY_PARKS, _WAKEABLE, Kind,
                               ended, occupies)
from dispatcher.claims import _box_free
from dispatcher.workspace import (MissingBaseError, create_workspace)
import telegram.inbound as inbound
from telegram.inbound import Command, Plain, Reply
from telegram.notify import Notifier
from telegram.templates import task_ref
from dispatcher.drive import (_admission_candidates, _drive_task, _driven, _flush_finished,
                             _wake_reached, _woken)
from dispatcher.executor import (_OPERATOR_ENDS, _Turn, _apply, _cancel_issue,
                                 _fail_task_crash, _report_provisioning_failure)
from dispatcher.launch_choice import (ClaimCandidate, Launch, _candidate_launch,
                                      _consume_execution_choice, _display_entry)
from dispatcher.stage_launch import (_end_session, _inject_login_code, _mark_wake_blocked,
                                     _queue_message, _ref,
                                     _spawn_stage, _sync_task_tokens, _task_token,
                                     _wake)


def _status_lines(cfg: Config) -> list[str]:
    repositories = {r.name: r for r in cfg.repositories}
    tasks = [t for t in load_all(cfg.state_dir) if _in_flight(t)]
    lines = ([_status_line(cfg, repositories.get(t.repository), t) for t in tasks]
             or ["(nothing in flight)"])
    return lines + _capacity_lines(cfg, tasks)


def _status_line(cfg: Config, repository: Repository | None, t: TaskState) -> str:
    """One in-flight task: ref, title, stage, model, a non-running kind, slot."""
    model = _display_entry(cfg, repository, t)
    slot = "(no slot)" if t.slot == NO_SLOT else f"(slot {t.slot})"
    kind = t.status.kind
    return (f"{_ref(cfg, t)} {t.title} — {t.stage.value} [{model}]"
            + (f" [{kind.value}]" if kind is not Kind.RUNNING else "") + f" {slot}")


def _capacity_lines(cfg: Config, tasks: list[TaskState]) -> list[str]:
    # The sweep holds a real capacity unit that occupies() cannot see (its work
    # is a herdr tab, not a TaskState) — _run_pass reduces effective
    # capacity for it, so status must count it too or it reports 1/2 on a full
    # box for as long as the sweep runs.
    held = 1 if triage.running() else 0
    lines = ["triage sweep running (holds 1 slot)"] if held else []
    return lines + [f"capacity {sum(occupies(t.status) for t in tasks) + held}/{cfg.capacity}"]


def _in_flight(t: TaskState) -> bool:
    """A task the pipeline still carries before its PR opens: not ended, not
    at pr-open."""
    return not ended(t.status) and t.stage is not Stage.PR_OPEN


def _find_rows(cfg: Config, deps: Deps, issue: int) -> list[tuple]:
    by_name = {r.name: r for r in cfg.repositories}
    return [(by_name[row["repository"]], row)
            for members in by_project(cfg.repositories).values()
            for row in deps.github.rank_rows(members)
            if row["number"] == issue]


def _queue_lines(cfg: Config, deps: Deps) -> list[str]:
    lines: list[str] = []
    projects = by_project(cfg.repositories)
    for name, members in projects.items():
        if len(projects) > 1:
            lines.append(f"[{name}]")
        lines += _rank_lines(deps.github.rank_rows(members), len(members) > 1)
    return lines


def _rank_lines(rows: list[dict], multi_repository: bool) -> list[str]:
    """One project's /queue lines; rows are named `repository#n` when the
    project has several repositories."""
    available = [r for r in rows
                 if not r["blocked"] and r.get("status") != "In progress"]
    return (_available_lines(available, multi_repository)
            + _summary_lines(rows, multi_repository))


def _row_ref(row: dict, multi_repository: bool) -> str:
    return task_ref(row["repository"], row["number"], multi_repository)


def _available_lines(available: list[dict], multi_repository: bool) -> list[str]:
    """The top ten workable rows, numbered, plus a count of the rest."""
    lines = [_rank_line(idx, r, multi_repository)
             for idx, r in enumerate(available[:10], start=1)]
    if len(available) > 10:
        lines.append(f"… {len(available) - 10} more")
    return lines


def _rank_line(idx: int, row: dict, multi_repository: bool) -> str:
    boost = row.get("boost", 0)
    marker = f" ↑{boost}" if boost > 0 else (f" ↓{-boost}" if boost < 0 else "")
    value = row.get("score")
    score = f"{value:.2f}" if value is not None else "—"
    return f"{idx}.{marker} [{score}] {_row_ref(row, multi_repository)} {row['title']}"


def _summary_lines(rows: list[dict], multi_repository: bool) -> list[str]:
    """The in-progress and blocked issue lists, or a note that the queue is
    empty."""
    lines = _issue_list("In progress", [r for r in rows
                                        if r.get("status") == "In progress"],
                        multi_repository)
    lines += _issue_list("Blocked", [r for r in rows if r["blocked"]],
                         multi_repository)
    if not rows:
        lines.append("(queue empty)")
    return lines


def _issue_list(label: str, rows: list[dict], multi_repository: bool) -> list[str]:
    """One "<label>: #1, #2" line, or none when there are no rows."""
    if not rows:
        return []
    return [f"{label}: " + ", ".join(_row_ref(r, multi_repository) for r in rows)]


def _locate(cfg: Config, deps: Deps, issue: int):
    """Resolve an issue to (repository, row); reports and returns None when it
    can't."""
    hits = _find_rows(cfg, deps, issue)
    if not hits:
        deps.notifier.send("status", lines=[f"#{issue} is not on the board"])
        return None
    if len(hits) > 1:
        deps.notifier.send("status", lines=[
            f"#{issue} exists in multiple repositories ("
            + ", ".join(t.name for t, _ in hits)
            + ") — not yet supported, edit the board directly"])
        return None
    return hits[0]


def _handle_boost(cfg: Config, deps: Deps, issue: int, amount: int) -> None:
    located = _locate(cfg, deps, issue)
    if not located:
        return
    repository, row = located
    plan = queue_ops.plan_boost(row, amount)
    queue_ops.apply_plan(deps.github, repository, issue, plan)
    deps.notifier.send("status", lines=plan.reason.split("\n"))


def _handle_next(cfg: Config, deps: Deps, issue: int, force: bool) -> None:
    located = _locate(cfg, deps, issue)
    if not located:
        return
    repository, row = located
    plan = queue_ops.plan_next(row, force)
    queue_ops.apply_plan(deps.github, repository, issue, plan)
    deps.notifier.send("status", lines=plan.reason.split("\n"))


# A failing gh or rank call answers in chat instead of crashing the pass.
_COMMAND_ERRORS = (subprocess.CalledProcessError, OSError, LookupError,
                   ValueError, BoardError)


def _reply_parked(tasks: list[TaskState]) -> list[TaskState]:
    return [t for t in tasks if t.status.kind in _REPLY_PARKS]


def _parked(t: TaskState) -> bool:
    """Parked, held at a login prompt, or woken from either: what a wake
    acts on."""
    return t.status.kind in _WAKEABLE or _woken(t)


def _handle_telegram(cfg: Config, deps: Deps, dry_run: bool = False) -> None:
    if dry_run:
        return
    tasks = load_all(cfg.state_dir)
    for ev in inbound.fetch_events(cfg.state_dir):
        if isinstance(ev, Command):
            _handle_command(cfg, deps, tasks, ev)
        elif isinstance(ev, Reply):
            _handle_reply(cfg, deps, tasks, ev)
        elif isinstance(ev, Plain):
            _handle_plain(cfg, deps, _reply_parked(tasks), ev.text)


def _handle_command(cfg: Config, deps: Deps, tasks: list[TaskState],
                    ev: Command) -> None:
    if ev.name == "status":
        deps.notifier.send("status", lines=_status_lines(cfg))
    elif ev.name == "attach":
        _handle_attach(cfg, deps, tasks, ev.issue)
    else:
        _handle_board_command(cfg, deps, ev)


def _handle_attach(cfg: Config, deps: Deps, tasks: list[TaskState],
                   issue: int) -> None:
    """A bare number names one task only while no other repository has the same
    number parked; when two do, wake nothing and list them."""
    match = [t for t in tasks if t.issue == issue and _parked(t)]
    if len(match) == 1:
        _wake(cfg, match[0], "", hold=True)
    else:
        deps.notifier.send("status", lines=_unattached_lines(cfg, issue, match))


def _unattached_lines(cfg: Config, issue: int, match: list[TaskState]) -> list[str]:
    """Why /attach woke nothing: several parked tasks share the number, or none."""
    if not match:
        return [f"#{issue} is not parked"]
    return ([f"#{issue} is ambiguous; parked tasks with that number:"]
            + [_ref(cfg, t) for t in match])


def _handle_board_command(cfg: Config, deps: Deps, ev: Command) -> None:
    """/queue, /boost and /next."""
    try:
        if ev.name == "queue":
            deps.notifier.send("queue", lines=_queue_lines(cfg, deps))
        elif ev.name == "boost":
            _handle_boost(cfg, deps, ev.issue, ev.amount)
        elif ev.name == "next":
            _handle_next(cfg, deps, ev.issue, ev.force)
    except _COMMAND_ERRORS as exc:
        what = "/queue" if ev.name == "queue" else f"#{ev.issue} {ev.name}"
        deps.notifier.send("status", lines=[f"{what} failed: {exc}"])


def _handle_reply(cfg: Config, deps: Deps, tasks: list[TaskState],
                  ev: Reply) -> None:
    login = _replied_to(_held_login(tasks), ev)
    match = _replied_to(_reply_parked(tasks), ev)
    if login:
        _inject_login_code(cfg, deps, login[0], ev.text)
    elif match:
        _wake(cfg, match[0], ev.text)
    else:
        deps.notifier.send("status",
                           lines=["(reply didn't match any parked task)"])


def _held_login(tasks: list[TaskState]) -> list[TaskState]:
    return [t for t in tasks if t.status.kind is Kind.HELD_LOGIN]


def _replied_to(tasks: list[TaskState], ev: Reply) -> list[TaskState]:
    """The tasks whose parked message `ev` replies to."""
    return [t for t in tasks if t.park_msg_id == ev.reply_to_msg_id]


def _handle_plain(cfg: Config, deps: Deps, reply_parked: list[TaskState],
                  text: str) -> None:
    if len(reply_parked) == 1:
        _wake(cfg, reply_parked[0], text)
    else:
        deps.notifier.send("status", lines=(
            ["Which task? Reply directly to its parked message:"]
            + [f"{_ref(cfg, t)} {t.title}" for t in reply_parked]))


# The weekly allowance grows continuously, so a box running at pace crosses
# zero headroom back and forth; the resume ping waits for this much to return.
RESUME_HEADROOM = 0.02


def _budget_edge(cfg: Config, deps: Deps, verdict: Verdict, now: datetime) -> None:
    """Edge-triggered stall/resume pings via a marker file, keyed on the
    verdict for the global policy default — what an idle box spawns next.
    Stall on the first denied pass; resume only once the default is admitted
    with at least RESUME_HEADROOM, or every zero crossing pings a pair.

    An unavailable provider also denies, but it is not a budget stall: there
    is no window and nothing will reset, so the "resumes when headroom
    returns" ping would send an operator off to wait out an outage that only
    a re-login ends — 30 minutes before the accurate auth_dark alert.
    _auth_dark_edge owns that case; stay silent for it."""
    if verdict.reason == "unavailable":
        return
    marker = Path(cfg.state_dir) / "budget-stalled"
    marker.parent.mkdir(parents=True, exist_ok=True)
    note = verdict_note(verdict, now)
    if not verdict.admitted and not marker.exists():
        marker.write_text(_now())
        deps.notifier.send("budget_stall", issue=0, title="(all tasks)", url="", note=note)
    elif (verdict.admitted and marker.exists()
          and (verdict.binding is None or verdict.binding.headroom >= RESUME_HEADROOM)):
        marker.unlink()
        deps.notifier.send("budget_resume", issue=0, title="(all tasks)", url="", note=note)


AUTH_DARK_GRACE_MINUTES = 30
AUTH_DARK_REALERT_HOURS = 8


def _auth_dark_edge(cfg: Config, deps: Deps, usages: dict[str, ProviderUsage]) -> None:
    """One alert per unknowable-usage incident, repeated every
    AUTH_DARK_REALERT_HOURS while it lasts (auth likely dead). Dark means
    EVERY fetched provider is unavailable; a single dark provider only fails
    its own models closed and stays visible on the console.

    Companion to _budget_edge: budget_stall covers "gate closed, headroom
    returns on its own"; this covers "we cannot even tell", which fail-safes
    every spawn indefinitely and therefore needs a human. The 30-minute grace
    absorbs transient API blips."""
    marker = Path(cfg.state_dir) / "auth-dark"
    dark = not usages or all(u.source == "unavailable" for u in usages.values())
    if not dark:
        marker.unlink(missing_ok=True)
        return
    if not marker.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"since": _now(), "alerted": False}))
        return
    now = datetime.now(timezone.utc)
    try:
        st = json.loads(marker.read_text())
        since = datetime.fromisoformat(st["since"])
        alerted = bool(st.get("alerted"))
        # legacy marker (alerted, no last_alert): count the interval from since
        last = datetime.fromisoformat(st["last_alert"]) if alerted and "last_alert" in st else since
        age_min = (now - since).total_seconds() / 60
        # subtracting here also rejects offset-free timestamps (TypeError)
        due = (now - last >= timedelta(hours=AUTH_DARK_REALERT_HOURS) if alerted
               else age_min >= AUTH_DARK_GRACE_MINUTES)
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
        marker.write_text(json.dumps({"since": _now(), "alerted": False}))
        return
    # send returns 0 on failure: keep the marker so the next pass retries
    if due and deps.notifier.send("auth_dark", minutes=int(age_min),
                                  host=socket.gethostname()):
        marker.write_text(json.dumps({"since": st["since"], "alerted": True,
                                      "last_alert": _now()}))


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


def _seat(cfg: Config, repository: Repository, task: TaskState) -> TaskState | None:
    """The task on a slot it may run in, under box capacity; None when it
    must wait (marked wake-blocked). Nothing is seated while a task file
    cannot be read: the task it holds may own a unit and a slot no count sees."""
    tasks = _readable_tasks(cfg)
    blocked = _seat_blocker(cfg, tasks)
    if tasks is None or blocked:
        _mark_wake_blocked(cfg, repository, task, blocked)
        return None
    return _seated(cfg, repository, task, tasks)


def _seat_blocker(cfg: Config, tasks: list[TaskState] | None) -> str:
    """Why no task can be seated now: "" when the box has room."""
    if tasks is None:
        return UNREADABLE_TASK_FILE
    return "capacity full" if _box_free(cfg, tasks) <= 0 else ""


def _seated(cfg: Config, repository: Repository, task: TaskState,
            tasks: list[TaskState]) -> TaskState | None:
    """The task on its own slot, or a free one; None (marked wake-blocked)
    when no slot is free."""
    others = [t for t in tasks
              if (t.repository, t.issue) != (task.repository, task.issue)]
    if not _slot_lost(task, others):
        return task
    # Parked tasks gave their slot back, and a recorded slot another
    # holder took is not ours. Take any free one: worktrees are
    # per-issue, so the number need not be the original.
    slot = allocate_slot(others, max_slots(cfg.capacity))
    if slot is None:
        _mark_wake_blocked(cfg, repository, task, "no free slot")
        return None
    return replace(task, slot=slot)


def _slot_lost(task: TaskState, others: list[TaskState]) -> bool:
    """Whether the task has no slot, or another holder took its recorded one."""
    return task.slot == NO_SLOT or any(occupies(t.status) and t.slot == task.slot
                                       for t in others)


def _reconcile_boards(cfg: Config, deps: Deps) -> None:
    """Every project's board, paused ones too, gets a card for each new open
    issue and its repositories the label set (#230 D8). A failure skips
    that project until the next pass."""
    tracks = labels.tracks(cfg)
    for name, members in by_project(cfg.repositories).items():
        try:
            synced = deps.github.reconcile_board(members, tracks)
        except Exception as exc:  # this project only: the pass goes on
            print(f"[warn] {name}: board reconcile failed — {exc}", file=sys.stderr)
            continue
        for line in synced.lines():
            print(f"[board] {name}: {line}")
        for error in synced.errors:
            print(f"[warn] {name}: board reconcile — {error}", file=sys.stderr)


def _no_task_tokens(deps: Deps) -> str:
    """Why no task token can be minted this pass, or "": the App is not
    registered (the infra README checklist) or the App errored."""
    try:
        registered = deps.access.app.registered()
    except github_app.AppError as exc:
        return f"the GitHub App is unavailable: {exc}"
    return "" if registered else f"the GitHub App is not registered: {github_app.README}"


def _ranked_claim_candidates(deps: Deps, members: list[Repository]):
    """Read one project's ranked candidates, warning when its turn cannot run."""
    why = _no_task_tokens(deps)
    if why:
        print(f"[warn] {members[0].project.name}: no claims this pass — {why}",
              file=sys.stderr)
        return ()
    try:
        return deps.github.candidates(members)
    except (BoardError, host_auth.HostWait) as exc:
        print(f"[warn] {members[0].project.name}: no claims this pass — {exc}",
              file=sys.stderr)
        return ()


def _quarantined(cfg: Config, deps: Deps, repository: Repository, cand: Candidate,
                 skipped: set[tuple[str, int]]) -> bool:
    """A blocker issue holds the card back; it lands in `skipped`."""
    held = failures.check_quarantine(cfg.state_dir, deps.github, repository.name, cand.number)
    if held:
        skipped.add((repository.name, cand.number))
    return held


def _ranked_again(cfg: Config, deps: Deps, by_issue: dict, repository: Repository,
                  cand: Candidate, pass_started: str) -> bool:
    """Whether a ranked card is claimed fresh: it has no task, or the table
    swept its ended task's file on `candidate-ranked`."""
    task = by_issue.get((repository.name, cand.number))
    if task is None:
        return True
    if _rank_rejected(cfg, task):
        return False
    this_pass = bool(pass_started) and task.updated_at >= pass_started
    turn = _Turn(cfg, deps, repository, admit=None, order=None, dry_run=False)
    outcome = _apply(turn, task, Event(EventType.CANDIDATE_RANKED, Ranked(this_pass)))
    if isinstance(outcome, Reject):
        _rank_rejected_path(cfg, task.repository, task.issue).write_text(task.updated_at)
    return _swept(outcome)


def _swept(outcome: Transition | Ignore | Reject) -> bool:
    return isinstance(outcome, Transition) and any(
        isinstance(a, SweepTombstone) for a in outcome.actions)


def _claimable(cfg: Config, deps: Deps, members: list[Repository], tasks: list[TaskState],
               admit: Admit, order: Order, pass_started: str,
               skipped: set[tuple[str, int]]) -> Iterator[ClaimCandidate]:
    """One project's ranked candidates this pass may claim, across its member
    repositories in rank order, each with its repository and the spec launch it
    would start. Skips quarantined issues, issues whose task stays (candidate-ranked),
    and candidates whose spec model has no headroom — a denied candidate
    leaves the next one eligible, since its model may differ."""
    by_name = {r.name: r for r in members}
    by_issue = {(t.repository, t.issue): t for t in tasks}
    for cand in _ranked_claim_candidates(deps, members):
        repository = by_name[cand.repository]
        if _quarantined(cfg, deps, repository, cand, skipped):
            continue
        launch = _fresh_claim_launch(cfg, deps, by_issue, repository, cand, admit, order,
                                     pass_started)
        if launch is not None:
            yield repository, cand, launch


def _fresh_claim_launch(cfg: Config, deps: Deps, by_issue: dict, repository: Repository,
                        cand: Candidate, admit: Admit, order: Order,
                        pass_started: str) -> Launch | None:
    """The spec launch a ranked card is claimed fresh with; None when its
    task stays or policy rejects it."""
    if not _ranked_again(cfg, deps, by_issue, repository, cand, pass_started):
        return None
    launch, _bypass = _candidate_launch(cfg, repository, cand, admit, order)
    return launch


class ClaimRoundOutcome(Enum):
    """How claiming one picked candidate ends for the round."""

    CONTINUE = auto()
    STOP = auto()
    PROJECT_FAILED = auto()
    CLAIMED = auto()


@dataclass
class ClaimRound:
    """Mutable state of one claim round."""

    free_slots: int
    projects: dict[str, list[Repository]]
    counts: dict[str, int]
    last_claimed: dict[str, str]
    caps: dict[str, int | None]
    candidates: dict[str, Iterator[ClaimCandidate]]


def _commit_claim(cfg: Config, deps: Deps, repository: Repository, cand: Candidate,
                  slot: int, wt: str, launch: Launch, base: str = "main") -> TaskState:
    """Board mutation is last and irreversible: state is saved and the
    `claimed` event appended first, so a crash before the claim leaves a
    recoverable partial claim rather than a silently lost slot. A failure
    from here on releases the board claim back to Ready. A repository the
    installation doesn't reach is claimed and parked; an App error leaves it
    claimed and waking(launch), for the next pass (_task_token)."""
    task = TaskState(issue=cand.number, repository=repository.name,
                     stage=Stage.QUEUED, slot=slot, worktree=wt,
                     branch=f"agent/task-{cand.number}",
                     title=cand.title, updated_at=_now(),
                     effort=cand.effort, labels=cand.labels, base=base,
                     track=track_from_labels(cand.labels, policy_for(cfg, repository)))
    save(cfg.state_dir, task)  # state exists BEFORE the irreversible claim, so a partial claim is recoverable
    eventlog.append_event(cfg.state_dir, "claimed", repository=repository.name,
                          issue=cand.number, stage=Stage.QUEUED.value)
    try:
        deps.github.claim(repository, cand)  # irreversible board mutation — last
        gh_config = _task_token(cfg, deps, repository, task)
        if gh_config is None:
            return load(cfg.state_dir, repository.name, cand.number) or task
        task = _spawn_stage(cfg, deps, repository, task, launch, gh_config=gh_config)
        _consume_execution_choice(cfg, repository.name, cand.number)
    except Exception:
        deps.github.release(repository, cand.number, "claim/spawn failed after provisioning")
        raise
    return task


def _wait_in_ready(cfg: Config, deps: Deps, repository: Repository, number: int,
                   key: str, why: str) -> None:
    """Leave the issue in Ready; ping the operator only the first time for this key."""
    if base_line.is_recorded(cfg.state_dir, repository.name, number, key):
        return
    # The notifier returns 0 on a failed send: record only a delivered ping, so a
    # failed one is retried on the next pass.
    if deps.notifier.send("status", lines=[f"#{number} waits in Ready: {why}"]):
        base_line.record_wait(cfg.state_dir, repository.name, number, key)


def _claim_picked(cfg: Config, deps: Deps, repository: Repository,
                  cand: Candidate, launch: Launch,
                  dry_run: bool) -> ClaimRoundOutcome:
    """Provision and commit one ranked candidate; return its round outcome."""
    base = _read_claim_base(cfg, deps, repository, cand)
    if base is None:
        return ClaimRoundOutcome.CONTINUE
    slot = allocate_slot(load_all(cfg.state_dir), max_slots(cfg.capacity))
    if slot is None:
        return ClaimRoundOutcome.STOP
    try:
        wt = create_workspace(repository, cand.number, base, dry_run=dry_run,
                              auth=deps.github.auth)
    except MissingBaseError:
        # Not claimed, not a failure: the issue stays Ready and every pass
        # asks again; the operator hears once per issue and base.
        _wait_in_ready(cfg, deps, repository, cand.number,
                       f"base:{base}", f"base branch {base} is not on origin")
        return ClaimRoundOutcome.CONTINUE
    except Exception:
        # Board never claimed (claim is last, still Ready) → no release
        # needed; report + quarantine, and the pass survives. Cap at one
        # provisioning failure per project per pass: a systemic fault (git
        # remote down, podman down, worktrees volume full) would otherwise
        # fail EVERY remaining Ready candidate. The project leaves this
        # pass's round; the next pass retries its remaining candidates.
        _report_provisioning_failure(cfg, deps, repository, cand, dry_run)
        return ClaimRoundOutcome.PROJECT_FAILED
    base_line.clear_wait(cfg.state_dir, repository.name, cand.number)
    _commit_claim(cfg, deps, repository, cand, slot, wt, launch, base)
    return ClaimRoundOutcome.CLAIMED


def _read_claim_base(cfg: Config, deps: Deps, repository: Repository,
                     cand: Candidate) -> str | None:
    """Read and validate a candidate's base line, recording invalid bases."""
    try:
        body = deps.github.issue_body(repository, cand.number)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError, ValueError) as exc:  # ValueError: gh printed malformed JSON
        # Unclaimed and still Ready: the next pass reads it again.
        print(f"[warn] body of #{cand.number} not read: {exc}", file=sys.stderr)
        return None
    base = base_line.parse_base(body)
    if base is None:
        _wait_in_ready(cfg, deps, repository, cand.number, "invalid",
                       "its base line names no valid branch")
        return None
    return base


def _claim_round_turn(cfg: Config, deps: Deps, state: ClaimRound,
                      dry_run: bool) -> bool:
    """Take one project turn; False when the round must stop."""
    name = claims.pick_project(list(state.projects), state.counts,
                               state.last_claimed, state.caps)
    if name is None:
        return False
    picked = next(state.candidates[name], None)
    if picked is None:
        del state.projects[name]
        return True
    repository, cand, launch = picked
    outcome = _claim_picked(cfg, deps, repository, cand, launch, dry_run)
    if outcome is ClaimRoundOutcome.STOP:
        return False
    if outcome is ClaimRoundOutcome.PROJECT_FAILED:
        del state.projects[name]
    if outcome is ClaimRoundOutcome.CLAIMED:
        state.counts[name] += 1
        state.last_claimed[name] = _now()
        state.free_slots -= 1  # checked before the next pull, so no candidate is vetted that cannot be claimed
    return True


def _claim_generators(
        cfg: Config, deps: Deps, in_round: dict[str, list[Repository]],
        all_tasks: list[TaskState], admit: Admit, order: Order,
        pass_started: str, skipped: set[tuple[str, int]],
) -> dict[str, Iterator[ClaimCandidate]]:
    """Lazy per-project candidate streams for the claim round."""
    return {name: _claimable(cfg, deps, members, all_tasks, admit, order,
                             pass_started, skipped)
            for name, members in in_round.items()}


def _claim_room(cfg: Config) -> tuple[list[TaskState], int] | None:
    """Every task and the free claim room; None when nothing may be claimed:
    no room, or a task file cannot be read (_readable_tasks)."""
    all_tasks = _readable_tasks(cfg)
    if all_tasks is None:
        return None
    free = claims.claim_room(cfg.capacity, cfg.max_open, all_tasks,
                             triage_running=False)
    return (all_tasks, free) if free > 0 else None


def _claim_round_data(cfg: Config, deps: Deps,
                      repositories: list[Repository], admit: Admit,
                      order: Order, pass_started: str,
                      skipped: set[tuple[str, int]]) -> ClaimRound | None:
    """Build one claim round's state, or None when there is no claim room."""
    room = _claim_room(cfg)
    if room is None:
        return None
    all_tasks, free = room
    in_round = by_project([r for r in repositories if not r.project.paused])
    project_of = {r.name: r.project.name for r in repositories}
    counts = claims.project_counts(list(in_round), all_tasks, project_of)
    last = claims.project_last_claims(claims.last_claims(cfg.state_dir), project_of)
    caps = {name: members[0].project.max_active for name, members in in_round.items()}
    # Generator bodies run on first next(), so a project's board is read only on its turn.
    gens = _claim_generators(cfg, deps, in_round, all_tasks, admit, order,
                             pass_started, skipped)
    return ClaimRound(free_slots=free, projects=in_round, counts=counts,
                      last_claimed=last, caps=caps, candidates=gens)


def _run_claim_round(cfg: Config, deps: Deps, repositories: list[Repository],
                     admit: Admit, order: Order, dry_run: bool,
                     pass_started: str, skipped: set[tuple[str, int]]) -> None:
    """Claim until capacity or candidates run out; quarantine skips land in `skipped`."""
    state = _claim_round_data(cfg, deps, repositories, admit, order,
                              pass_started, skipped)
    while state is not None and state.free_slots > 0:
        if not _claim_round_turn(cfg, deps, state, dry_run):
            return


def _claim_new(cfg: Config, deps: Deps, repositories: list[Repository],
               admit: Admit, order: Order, dry_run: bool,
               pass_started: str = "") -> None:
    """Claim free units one at a time, each via claims.pick_project. A paused
    project claims nothing; a project leaves the round when its candidates run
    out or provisioning fails for it. Nothing is claimed while a task file
    cannot be read: the task it holds may own the issue a claim would take
    again, and a capacity unit and a slot no count can see."""
    skipped: set[tuple[str, int]] = set()
    try:
        _run_claim_round(cfg, deps, repositories, admit, order, dry_run,
                         pass_started, skipped)
    finally:
        failures.write_quarantine_verdict(cfg.state_dir, pass_started, skipped)


def _task_for_intent(cfg: Config, intent: intents.Intent) -> TaskState | None:
    """Resolve an intent to the task it repositories. A repository-carrying intent
    (every intent the console writes now) is an exact lookup. A legacy
    intent (repository == "", written before this keying existed) falls back to
    matching by issue number alone — ambiguous when more than one repository has
    a task with that number, in which case there is no safe task to act on
    and the intent is skipped rather than guessed at."""
    if intent.repository:
        return load(cfg.state_dir, intent.repository, intent.issue)
    hits = [t for t in load_all(cfg.state_dir) if t.issue == intent.issue]
    return hits[0] if len(hits) == 1 else None   # ambiguous legacy → skip


def _apply_reply_intent(cfg: Config, deps: Deps, by_name: dict, task: TaskState | None,
                        intent: intents.Intent) -> None:
    """Deliver a reply, or inject it when a login prompt owns the pane
    (outside the table until ticket 11)."""
    if task is not None and task.status.kind is Kind.HELD_LOGIN:
        _inject_login_code(cfg, deps, task, intent.payload.get("text", ""))
    else:
        _deliver_reply(cfg, deps, by_name, task, intent)


def _deliver_reply(cfg: Config, deps: Deps, by_name: dict, task: TaskState | None,
                   intent: intents.Intent) -> None:
    """Queue the reply for its task's session, then let the table decide
    the reply: a rejected one leaves the message queued, undelivered."""
    repository = _reply_repository(cfg, task, intent)
    if not repository:
        print(f"[warn] reply for #{intent.issue} dropped: legacy intent "
              f"names no repository", file=sys.stderr)
        return
    _queue_message(cfg, repository, intent.issue, intent.payload.get("text", ""),
                   intent.actor or "operator")
    if task is not None:
        _apply_operator_intent(cfg, deps, by_name, task, intent)


def _reply_repository(cfg: Config, task: TaskState | None,
                      intent: intents.Intent) -> str:
    """The reply's repository. A legacy intent (repository "") names its
    project only through its task, or through the sole configured repository;
    otherwise it is not guessed at ("")."""
    return (intent.repository or (task.repository if task is not None else "")
            or (cfg.repositories[0].name if len(cfg.repositories) == 1 else ""))


def _apply_answers_intent(cfg: Config, deps: Deps, by_name: dict, task: TaskState | None,
                          intent: intents.Intent) -> None:
    """Write the answers file of the task's open request; a submission
    then queues its message and the table decides the wake."""
    reason = answers.drop_reason(task, intent)
    if reason:
        raise IntentDropped(reason)
    assert task is not None
    wake = answers.record(task, intent)
    if wake:
        task = replace(task, plan_approval=answers.approval(task, intent))
        _queue_message(cfg, task.repository, task.issue, wake, intent.actor or "operator")
        _apply_operator_intent(cfg, deps, by_name, task, intent)


# The operator intents the table decides (ticket 08).
def _apply_operator_intent(cfg: Config, deps: Deps, by_name: dict,
                           task: TaskState | None, intent: intents.Intent) -> None:
    """An operator intent the table decides for the task; a refusal is
    written as `transition-rejected`. Without a task file or a configured
    repository the table has nothing to decide on: a kill or a cancel still
    ends what it can (_end_untracked), any other intent is skipped."""
    repository = by_name.get(task.repository) if task is not None else None
    if repository is not None:
        _decide_intent(cfg, deps, repository, task, intent)
    elif OPERATOR_INTENTS[intent.action] in _OPERATOR_ENDS:
        _end_untracked(cfg, deps, by_name, task, intent)
    else:
        print(f"[warn] {intent.action} intent for #{intent.issue}: no task in a configured "
              f"repository — skipped", file=sys.stderr)


def _decide_intent(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                   intent: intents.Intent) -> None:
    """The table's turn for the intent; raises IntentRefused when it refuses."""
    turn = _Turn(cfg, deps, repository, admit=None, order=None, dry_run=False,
                 actor=intent.actor or "operator")
    if isinstance(_apply(turn, task, operator_event(intent.action, intent.payload)), Reject):
        raise IntentRefused(intent.action)


def _end_untracked(cfg: Config, deps: Deps, by_name: dict,
                   task: TaskState | None, intent: intents.Intent) -> None:
    """A kill or a cancel the table cannot take: the session ends, and a
    cancel still closes the issue of a configured repository."""
    repository = _intent_repository(cfg, intent)
    if not repository:
        log.warning("%s intent for #%d: no unique matching task — skipped",
                    intent.action, intent.issue)
        return
    _end_session(cfg, deps, repository, intent.issue)
    if intent.action == "cancel" and repository in by_name:
        _cancel_issue(deps, by_name[repository], intent.issue)
    _record_untracked_end(cfg, task, intent, repository)


def _record_untracked_end(cfg: Config, task: TaskState | None, intent: intents.Intent,
                          repository: str) -> None:
    """A task file (of a repository that left the config) is ended in place;
    the console's history reads the end either way."""
    stage = ""
    status, detail = _OPERATOR_ENDS[OPERATOR_INTENTS[intent.action]]
    if task is not None:
        save(cfg.state_dir, replace(end(task, status), updated_at=_now()))
        stage = task.stage.value
    kind = status.kind.value
    eventlog.append_event(cfg.state_dir, kind, repository=repository, issue=intent.issue,
                          stage=stage, actor=intent.actor, detail=detail)


def _apply_retry_intent(cfg: Config, issue: int) -> None:
    for repository in cfg.repositories:
        qp = failures.quarantine_path(cfg.state_dir, repository.name, issue)
        rec = failures.read_quarantine(qp)
        if rec is failures.GONE:
            continue
        fp = rec.get("fingerprint") if isinstance(rec, dict) else None
        if fp:
            failures.fingerprint_path(cfg.state_dir, fp).unlink(missing_ok=True)
        qp.unlink(missing_ok=True)


class IntentRefused(Exception):
    """The table refused the intent: its `transition-rejected` event says why."""


class IntentDropped(Exception):
    """An intent the dispatcher refuses: dropped with an event naming why."""

    def __init__(self, detail: str, model: str = ""):
        super().__init__(detail)
        self.model = model


def _require_resume_model(cfg: Config, by_name: dict,
                          task: TaskState | None, event: Event) -> None:
    """The authoritative check of a resume intent's model (an intent file can
    bypass the console's 422): configured for the repository, and of the stage
    pick's provider. Raises IntentDropped. A resume the table refuses is not
    checked: its refusal is the one the operator reads."""
    model = event.detail.model
    if not model or task is None or isinstance(decide(task, event), Reject):
        return
    repository = by_name.get(task.repository)
    policy = policy_for(cfg, repository)
    if model not in policy.model_ids():
        raise IntentDropped(f"model {model!r} is not configured for repository "
                            f"{task.repository!r}", model)
    refusal = override_refusal(task.picks, next_stage(task), model)
    if refusal:
        raise IntentDropped(refusal, model)


def _apply_resume_intent(cfg: Config, deps: Deps, by_name: dict,
                         task: TaskState | None, intent: intents.Intent) -> None:
    """Let the table decide the resume; its wake's launch tells the session
    (_resume_text)."""
    _require_resume_model(cfg, by_name, task, operator_event(intent.action, intent.payload))
    _apply_operator_intent(cfg, deps, by_name, task, intent)


def _apply_one_intent(cfg: Config, deps: Deps, by_name: dict,
                      intent: intents.Intent) -> None:
    """Route one operator intent to its action-specific handler."""
    issue = intent.issue
    task = _task_for_intent(cfg, intent)
    if intent.action == "reply":
        _apply_reply_intent(cfg, deps, by_name, task, intent)
    elif intent.action == "answers":
        _apply_answers_intent(cfg, deps, by_name, task, intent)
    elif intent.action == "resume":
        _apply_resume_intent(cfg, deps, by_name, task, intent)
    elif intent.action in OPERATOR_INTENTS:
        _apply_operator_intent(cfg, deps, by_name, task, intent)
    elif intent.action == "retry":
        _apply_retry_intent(cfg, issue)
    else:
        print(f"[warn] unknown intent action {intent.action!r} for #{issue}",
              file=sys.stderr)


def _intent_repository(cfg: Config, intent: intents.Intent) -> str:
    """The repository an intent's event names. A legacy intent (repository == "")
    carries no repository of its own; if it resolves unambiguously to one task,
    use THAT task's repository so the console's /task/{repository}/{issue} link is
    never blank. Same fallback idiom as the "kill" action's kill_repository."""
    resolved = _task_for_intent(cfg, intent)
    return intent.repository or (resolved.repository if resolved is not None else "")


def _apply_intents(cfg: Config, deps: Deps) -> None:
    """Drain operator intents (web console writes) at the top of the pass.
    Applied-then-deleted = at-most-once; a failed intent is deleted too,
    noted to stderr, and never aborts the pass or the remaining intents."""
    by_name = {t.name: t for t in cfg.repositories}
    pending = answers.without_superseded(intents.list_intents(cfg.state_dir),
                                         lambda i: _task_for_intent(cfg, i))
    for intent in pending:
        try:
            _apply_one_intent(cfg, deps, by_name, intent)
            eventlog.append_event(cfg.state_dir, "intent-applied",
                                  repository=_intent_repository(cfg, intent),
                                  issue=intent.issue, actor=intent.actor,
                                  detail=intent.action)
        except IntentRefused:
            pass   # its transition-rejected event is the record
        except IntentDropped as dropped:
            print(f"[warn] intent {intent.path.name}: {dropped}",
                  file=sys.stderr)
            eventlog.append_event(cfg.state_dir, "intent-dropped",
                                  repository=_intent_repository(cfg, intent),
                                  issue=intent.issue, model=dropped.model,
                                  actor=intent.actor, detail=str(dropped))
        except Exception as exc:
            print(f"[warn] intent {intent.path.name} failed: {exc}",
                  file=sys.stderr)
        finally:
            intents.delete_intent(intent)


SNAPSHOT_TTL_SECONDS = 7 * 24 * 3600


def _write_heartbeat(cfg: Config, started_at: str) -> None:
    """Atomic pass timestamp for the console's next-pass countdown. Written
    only when a pass completes: a crash-looping dispatcher goes stale, which
    the console surfaces as 'dispatcher not running?'. Never raises into a
    pass (same contract as eventlog.append_event)."""
    try:
        p = Path(cfg.state_dir) / "pass.json"
        write_json_atomic(p, {
            "started_at": started_at, "finished_at": _now(),
            "interval_minutes": cfg.pass_interval_minutes})
    except OSError as exc:
        print(f"[warn] heartbeat write failed: {exc}", file=sys.stderr)


def _prune_snapshots(cfg: Config) -> None:
    """A snapshot outlives its task by at most a week. In-flight tasks keep
    theirs regardless of age — the console may need it while they are
    parked. Every pass checks; ~200 KB per file makes eagerness cheap.

    The live set is built from state, not parsed from the stem: a
    "task-<repository>-<issue>.txt" stem can't be split back into (repository,
    issue) unambiguously (a repository name may itself contain digits and
    hyphens), so identity comes from load_all() same as everywhere else,
    same as _task_for_intent. The legacy "task-<issue>.txt" form is kept
    alive too, for any snapshot still sitting under its pre-rename name."""
    root = Path(cfg.state_dir) / "snapshots"
    if not root.exists():
        return
    live = _live_snapshot_names(cfg)
    if live is None:
        return  # unreadable/corrupt state dir: skip, never abort the sweep
    now = time.time()
    for p in root.glob("task-*.txt"):
        if p.name not in live:
            _unlink_if_expired(p, now)


def _live_snapshot_names(cfg: Config) -> set[str] | None:
    """The snapshot names of in-flight tasks, under both the current and
    the legacy form; None when the state dir cannot be read."""
    try:
        in_flight = [t for t in load_all(cfg.state_dir) if _in_flight(t)]
    except Exception:
        return None
    return ({f"task-{t.repository}-{t.issue}.txt" for t in in_flight}
            | {f"task-{t.issue}.txt" for t in in_flight})


def _unlink_if_expired(p: Path, now: float) -> None:
    """Delete a snapshot older than the TTL; one that vanished or cannot be
    read is left alone."""
    try:
        if now - p.stat().st_mtime > SNAPSHOT_TTL_SECONDS:
            p.unlink()
    except OSError:
        pass


@contextmanager
def _sandboxed_state(cfg: Config):
    """Yield `cfg` with state_dir rebound to a throwaway copy of the real one.

    --dry-run stubs every I/O *dependency* (GitHubClient, Sessions, Notifier,
    remove_workspace), but the pass also writes locally — state files, the
    event log, waiting markers, failure reports, the intent queue,
    the per-provider usage cache — and those writes were unguarded, so a dry run advanced
    tasks to terminal stages and wrote history for real. Guarding each call
    site is what already rotted: `dry_run` is threaded by hand into some
    functions and was never added to the flush or the wake path.

    Redirecting the one directory they all write through is the guard that
    cannot be forgotten, and it covers write sites added later for free. The
    pass still reads a faithful copy, so its decisions — and the [dry-run]
    lines it prints — are exactly the ones a real pass would make."""
    real = Path(cfg.state_dir)
    with tempfile.TemporaryDirectory(prefix="agent-ops-dry-run-") as tmp:
        sandbox = Path(tmp) / "state"
        if real.is_dir():
            shutil.copytree(real, sandbox)
        else:
            sandbox.mkdir(parents=True)
        yield replace(cfg, state_dir=str(sandbox))


def _starving(task: TaskState) -> bool:
    """Is this task legitimately still waiting for the capacity/slot it was
    denied? Exactly the queues that mark the marker, which wake it: a
    waking task (the wakes the table blocks, _wake_room). Anything else is not waiting for
    anything."""
    return task.status.kind is Kind.WAKING


def _migrate_legacy_keys(cfg: Config) -> None:
    """Take over pre-multi-project issue-only keys. Messages: see
    messages.migrate_legacy. Wake-blocked markers are transient (re-marked
    next pass if still true), so with several repositories a legacy one is just
    dropped; with one it is renamed to keep the edge-triggered event quiet."""
    names = [t.name for t in cfg.repositories]
    messages.migrate_legacy(cfg.state_dir, names)
    for p in Path(cfg.state_dir).glob(f"{WAKE_BLOCKED_PREFIX}*"):
        issue = p.name.removeprefix(WAKE_BLOCKED_PREFIX)
        if not issue.isdigit():
            continue
        if len(names) == 1:
            p.replace(_wake_blocked_path(cfg, names[0], int(issue)))
        else:
            p.unlink(missing_ok=True)


def _reconcile_wake_blocked(cfg: Config) -> None:
    """Re-establish from disk, at the top of every pass, that the wake-blocked
    marker (and the "waiting for a free slot" badge and delivery_contract
    suffix it drives) stands only for a task that is _starving(): no
    terminal path has to remember it, and a re-claim of the same issue never
    inherits a stale badge. A marker whose task file is gone goes too. (A
    slot needs no such pass: the save gives it back with the status.)"""
    known: set[str] = set()
    for task in load_all(cfg.state_dir):
        known.add(_wake_blocked_path(cfg, task.repository, task.issue).name)
        if not _starving(task):
            _clear_wake_blocked(cfg, task.repository, task.issue)
    for p in Path(cfg.state_dir).glob(f"{WAKE_BLOCKED_PREFIX}*"):
        if p.name not in known:  # state file flushed/deleted under the marker
            p.unlink(missing_ok=True)


def run_pass(cfg: Config, deps: Deps, dry_run: bool = False,
             config_path: str = "targets.yaml") -> None:
    if dry_run:
        with _sandboxed_state(cfg) as sandboxed:
            _run_pass(sandboxed, deps, dry_run, config_path)
        return
    _run_pass(cfg, deps, dry_run, config_path)


def _sync_artifacts(cfg: Config, *, dry_run: bool = False,
                    auth: host_auth.HostAuth = host_auth.STORED) -> None:
    repos = {repository.name: repository.repo for repository in cfg.repositories}
    for task in load_all(cfg.state_dir):
        try:
            task_artifacts.collect(cfg.state_dir, task, repos.get(task.repository, ""),
                                   publish=not dry_run and not ended(task.status),
                                   auth=auth)
        except (OSError, ValueError, host_auth.HostWait):
            log.exception("Artifact collection failed for %s/%s", task.repository, task.issue)
    task_artifacts.cleanup(cfg.state_dir)


def _begin_pass_auth(cfg: Config, deps: Deps) -> None:
    """The pass's App access and the host credentials it gives each project."""
    deps.access = _access(deps)
    projects: dict[str, list[str]] = {}
    for r in cfg.repositories:
        projects.setdefault(r.project.name, []).append(r.repo)
    deps.github.auth = host_auth.begin(list(projects.values()), deps.access)


def _run_pass(cfg: Config, deps: Deps, dry_run: bool = False,
              config_path: str = "targets.yaml") -> None:
    pass_started = _now()
    _begin_pass_auth(cfg, deps)
    _migrate_legacy_keys(cfg)
    _reconcile_wake_blocked(cfg)
    if not dry_run:
        _housekeep(cfg, deps, config_path)
    # Evaluate each triage signal once — triage.running() is a real herdr
    # subprocess; calling it per repository would spawn (1 + len(repositories)) processes
    # each pass. The two signals are semantically distinct: eff is reduced when
    # the sweep is running (it holds a real capacity unit occupies() cannot see);
    # claims are paused when any request is in flight (pending = file OR running).
    sweep_running = triage.running()
    occupied = int(sweep_running) + onboarding_sessions.running(cfg.state_dir)
    eff = replace(cfg, capacity=max(0, cfg.capacity - occupied))
    claims_paused = triage.pending(cfg.state_dir)
    _handle_telegram(cfg, deps, dry_run)
    usages = fetch_all(cfg)
    now = datetime.now(timezone.utc)
    admit: Admit = lambda model: admits(usages, model, now, cfg.pace)
    # Ranked on the reading the gate judges; the mode is read once per pass,
    # so a change applies from the next one.
    order = priority.order(priority.load(cfg.state_dir, routed_providers(cfg)),
                           usages, now, cfg.pace)
    default_verdict = admit(cfg.models.gate_entry().model_id)
    _budget_edge(cfg, deps, default_verdict, now)
    _auth_dark_edge(cfg, deps, usages)
    # Phase 1: work already in progress, on every repository, before any claim —
    # so one repository's new claims never starve another's approved spec.
    _drive_in_progress(eff, deps, admit, order, dry_run)
    _wake_reached(eff, deps)
    _drive_wakes(eff, deps, admit, order, dry_run)
    if not dry_run:
        launched = onboarding_sessions.tick(
            cfg, deps, admit, order, config_path,
            free=_box_free(eff, load_all(cfg.state_dir)))
        eff = replace(eff, capacity=max(0, eff.capacity - launched))
        _reconcile_boards(cfg, deps)
    # Phase 2: new claims, with whatever capacity phase 1 and inspection left.
    if not claims_paused:
        _claim_new(eff, deps, eff.repositories, admit, order, dry_run, pass_started)
    _sync_artifacts(cfg, dry_run=dry_run, auth=deps.github.auth)
    _flush_finished(cfg, deps, dry_run)
    _write_heartbeat(cfg, pass_started)


def _housekeep(cfg: Config, deps: Deps, config_path: str) -> None:
    """A real pass's upkeep before any work: tokens, intents, snapshots,
    onboarding sessions, and the triage sweep on what they leave."""
    _sync_task_tokens(cfg, deps)
    _apply_intents(cfg, deps)
    _prune_snapshots(cfg)
    onboarding_sessions.reconcile(cfg.state_dir)
    reserved = onboarding_sessions.running(cfg.state_dir)
    triage.tick(replace(cfg, capacity=max(0, cfg.capacity - reserved)), deps, config_path)


def _drive_in_progress(cfg: Config, deps: Deps, admit: Admit, order: Order,
                       dry_run: bool) -> None:
    """Drive every repository's in-progress tasks, then its CI wakes and PRs."""
    for repository in cfg.repositories:
        for task in [t for t in load_all(cfg.state_dir)
                     if t.repository == repository.name and _driven(t)]:
            _drive_guarded(cfg, deps, repository, task, admit, order, dry_run)
        _wake_ci(cfg, deps, repository)
        _poll_prs(cfg, deps, repository, dry_run)


def _drive_wakes(cfg: Config, deps: Deps, admit: Admit, order: Order,
                 dry_run: bool = False) -> None:
    """Drive every waking task box-wide, oldest first: capacity is shared,
    so repository listing order must not decide whose wake waits."""
    for repository, task in _admission_candidates(cfg, _woken):
        _drive_guarded(cfg, deps, repository, task, admit, order, dry_run)


def _drive_guarded(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                   admit: Admit, order: Order, dry_run: bool) -> None:
    """Drive one task's turn; a crash fails that task, not the pass."""
    try:
        _drive_task(cfg, deps, repository, task, admit, order, dry_run)
    except host_auth.HostWait as exc:   # the turn waits for the App, not a crash
        print(f"[warn] #{task.issue}: {exc}; its turn waits for the next pass",
              file=sys.stderr)
    except Exception:
        _fail_task_crash(cfg, deps, repository, task, dry_run)


def guarded_pass(cfg: Config, deps: Deps, config_path: str,
                 dry_run: bool = False) -> None:
    try:
        run_pass(cfg, deps, dry_run=dry_run, config_path=config_path)
    except Exception:
        rep = failures.FailureReport(
            klass="pass-crash", repository="", issue=0, title="(dispatcher)",
            error=traceback.format_exc(), log_tail="",
            repro=f"agent-ops-dispatcher --config {config_path}",
            worktree="")
        failures.report_failure(cfg, deps, rep, dry_run=dry_run)
        raise  # systemd must still see the unit fail


def send_digest(cfg: Config, deps: Deps) -> None:
    deps.notifier.send("daily_digest", lines=_status_lines(cfg))


def _load(config_path: str) -> tuple[Config, Config]:
    """targets.yaml as loaded, and that with the console's stored model
    settings in force; warns when the stored ones are rejected."""
    boot = load_config(config_path)
    cfg = apply_stored_models(boot)
    if cfg.models_origin.error:
        print(f"[warn] {cfg.models_origin.error}", file=sys.stderr)
    return boot, cfg


def _warn_unadapted(cfg: Config) -> None:
    missing = referenced_providers(cfg) - set(ADAPTERS)
    if missing:
        print(f"[warn] models: provider(s) {sorted(missing)} have no usage adapter; "
              f"their entries are never admitted", file=sys.stderr)


def _enqueue_triage(cfg: Config) -> None:
    if triage.enqueue(cfg.state_dir):
        print("triage request enqueued")
    else:
        print("triage already pending or running; not enqueued")


def _deps(cfg: Config, dry_run: bool) -> Deps:
    return Deps(github=GitHubClient(dry_run=dry_run), app=github_app.Client(),
                sessions=Sessions(dry_run=dry_run, memory=cfg.session_memory,
                                  cpus=cfg.session_cpus, state_dir=cfg.state_dir,
                                  clones={t.name: t.clone_path for t in cfg.repositories}),
                notifier=Notifier(dry_run=dry_run,
                                  console_url=cfg.console_url,
                                  multi_repository=len(cfg.repositories) > 1))


def _migrate_tmux(cfg: Config) -> None:
    """Called by agent-ops-infra/provision/update.sh, which already holds
    convergence.lock (the same file pass_lock flocks — taking it here again
    would block forever, flock being per open-file-description). Never run
    by hand while the dispatcher timer is live."""
    for line in tmux_migration.migrate(
            cfg.state_dir, lambda task, text: _wake(cfg, task, text)):
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-ops-dispatcher")
    ap.add_argument("--config", default="targets.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--triage", action="store_true")
    ap.add_argument("--triage-run", action="store_true")
    ap.add_argument("--migrate-tmux", action="store_true")
    args = ap.parse_args()
    boot, cfg = _load(args.config)
    _warn_unadapted(cfg)
    deps = _deps(cfg, args.dry_run)
    migrate.refuse_legacy_task_files(cfg.state_dir, args)
    _run_mode(args, boot, cfg, deps)


def _run_mode(args: argparse.Namespace, boot: Config, cfg: Config, deps: Deps) -> None:
    if args.digest:
        send_digest(cfg, deps)
    elif args.triage:
        _enqueue_triage(cfg)
    elif args.triage_run:
        triage.guarded_sweep(cfg, deps)
    elif args.migrate_tmux:
        _migrate_tmux(cfg)
    else:
        with pass_lock(cfg.state_dir):
            # The console stores model settings under this lock: read them
            # again inside it, so a pass never runs a block the console has
            # replaced (a removed track would park the tasks it claims).
            guarded_pass(apply_stored_models(boot), deps, args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
