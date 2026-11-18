"""Operator chat: Telegram commands and replies, and the status, rank and
queue text they answer with."""
from __future__ import annotations

import subprocess

import telegram.inbound as inbound
from dispatcher import queue_ops, triage
from dispatcher.board import BoardError
from dispatcher.config import Config, Repository, by_project
from dispatcher.deps import Deps
from dispatcher.drive import _woken
from dispatcher.launch_choice import _display_entry
from dispatcher.stage_launch import _inject_login_code, _ref, _wake
from dispatcher.state import NO_SLOT, Stage, TaskState, load_all
from dispatcher.status import _REPLY_PARKS, _WAKEABLE, Kind, ended, occupies
from telegram.inbound import Command, Plain, Reply
from telegram.templates import task_ref


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
        _handle_event(cfg, deps, tasks, ev)


def _handle_event(cfg: Config, deps: Deps, tasks: list[TaskState], ev) -> None:
    """One inbound Telegram event: a command, a reply, or plain text."""
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
        _run_board_command(cfg, deps, ev)
    except _COMMAND_ERRORS as exc:
        what = "/queue" if ev.name == "queue" else f"#{ev.issue} {ev.name}"
        deps.notifier.send("status", lines=[f"{what} failed: {exc}"])


def _run_board_command(cfg: Config, deps: Deps, ev: Command) -> None:
    if ev.name == "queue":
        deps.notifier.send("queue", lines=_queue_lines(cfg, deps))
    elif ev.name == "boost":
        _handle_boost(cfg, deps, ev.issue, ev.amount)
    elif ev.name == "next":
        _handle_next(cfg, deps, ev.issue, ev.force)


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
