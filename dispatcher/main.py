"""One dispatcher pass: candidates → capacity → budget → claim → drive.

Stateless per pass — the view is rebuilt every time from the board (via
candidates), state files, stage.json signals, and tmux liveness. Run by a
systemd timer; one invocation = one pass."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from dispatcher.budget import fetch_usage, should_spawn
from dispatcher.convergence import pass_lock
from dispatcher.config import Config, Target, load_config
from dispatcher.github import GitHubClient
from dispatcher.machine import (HandleCrash, NoOp, Notify, ParkForCI,
                                ParkForInput, SetTaskStage, SpawnStage,
                                next_actions)
from dispatcher.prompts import render_stage_prompt
from dispatcher.sessions import Sessions
from dispatcher.state import (IN_FLIGHT_STAGES, PARK_CI, PARK_HUMAN, PARK_WAKE,
                              Stage, TaskState, active, allocate_slot,
                              clear_waiting, has_waiting, load_all,
                              read_stage_signal, save)
from dispatcher.workspace import create_workspace
import telegram.inbound as inbound
from telegram.inbound import Command, Plain, Reply
from telegram.notify import Notifier


@dataclass
class Deps:
    github: object
    sessions: object
    notifier: object


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _url(target: Target, issue: int) -> str:
    return f"https://github.com/{target.repo}/issues/{issue}"


def _wake(cfg: Config, task: TaskState, text: str, hold: bool = False) -> None:
    save(cfg.state_dir, replace(task, park=PARK_WAKE, pending_reply=text,
                                hold_for_attach=hold, updated_at=_now()))


def _status_lines(cfg: Config) -> list[str]:
    tasks = [t for t in load_all(cfg.state_dir) if t.stage in IN_FLIGHT_STAGES]
    lines = [f"#{t.issue} {t.title} — {t.stage.value}"
             + (f" [{t.park}]" if t.park else "") + f" (slot {t.slot})"
             for t in tasks] or ["(nothing in flight)"]
    lines.append(f"capacity {len(active(tasks))}/{cfg.capacity}")
    return lines


NEXT_BOOST = 99


def _find_rows(cfg: Config, deps: Deps, issue: int) -> list[tuple]:
    return [(target, row)
            for target in cfg.targets
            for row in deps.github.rank_rows(target)
            if row["number"] == issue]


def _queue_lines(cfg: Config, deps: Deps) -> list[str]:
    lines: list[str] = []
    for target in cfg.targets:
        rows = deps.github.rank_rows(target)
        if len(cfg.targets) > 1:
            lines.append(f"[{target.name}]")
        available = [r for r in rows
                     if not r["blocked"] and r.get("status") != "In progress"]
        for idx, r in enumerate(available[:10], start=1):
            boost = r.get("boost", 0)
            marker = f" ↑{boost}" if boost > 0 else (f" ↓{-boost}" if boost < 0 else "")
            value = r.get("score")
            score = f"{value:.2f}" if value is not None else "—"
            lines.append(f"{idx}.{marker} [{score}] #{r['number']} {r['title']}")
        if len(available) > 10:
            lines.append(f"… {len(available) - 10} more")
        in_progress = [r for r in rows if r.get("status") == "In progress"]
        if in_progress:
            lines.append("In progress: "
                         + ", ".join(f"#{r['number']}" for r in in_progress))
        blocked = [r for r in rows if r["blocked"]]
        if blocked:
            lines.append("Blocked: "
                         + ", ".join(f"#{r['number']}" for r in blocked))
        if not rows:
            lines.append("(queue empty)")
    return lines


def _locate(cfg: Config, deps: Deps, issue: int):
    """Resolve an issue to (target, row); reports and returns None when it
    can't."""
    hits = _find_rows(cfg, deps, issue)
    if not hits:
        deps.notifier.send("status", lines=[f"#{issue} is not on the board"])
        return None
    if len(hits) > 1:
        deps.notifier.send("status", lines=[
            f"#{issue} exists in multiple targets ("
            + ", ".join(t.name for t, _ in hits)
            + ") — not yet supported, edit the board directly"])
        return None
    return hits[0]


def _handle_boost(cfg: Config, deps: Deps, issue: int, amount: int) -> None:
    located = _locate(cfg, deps, issue)
    if not located:
        return
    target, row = located
    current = row.get("boost", 0)
    new = current + amount
    deps.github.set_boost(target, issue, new)
    deps.notifier.send("status", lines=[f"#{issue} boost {current} → {new}"])


def _handle_next(cfg: Config, deps: Deps, issue: int, force: bool) -> None:
    located = _locate(cfg, deps, issue)
    if not located:
        return
    target, row = located
    if row["blocked"]:
        deps.notifier.send("status", lines=[
            f"#{issue} is blocked — resolve its blockers first "
            "(blocked issues cannot be forced)"])
        return
    if row.get("status") == "In progress":
        # The board is the double-dispatch guard: flipping an In-progress
        # issue back to Ready would let this pass claim work already in
        # flight. Never forceable.
        deps.notifier.send("status", lines=[
            f"#{issue} is already In progress — work on it is already in "
            "flight (in-progress issues cannot be forced)"])
        return
    problems = []
    if row.get("status") != "Ready":
        problems.append(f"status is {row.get('status') or 'unset'}, not Ready")
    if "auto" not in row.get("labels", []):
        problems.append("missing the auto label")
    if problems and not force:
        deps.notifier.send("status", lines=[
            f"#{issue} is not eligible: " + "; ".join(problems) + ".",
            f"Send /next {issue} force to make it eligible and enqueue."])
        return
    # Boost FIRST: it is the mutation most likely to fail (the Boost field may
    # not exist on the board yet), and failing before status/label keeps a
    # failed /next a clean no-op rather than a half-eligible issue.
    deps.github.set_boost(target, issue, NEXT_BOOST)
    if row.get("status") != "Ready":
        deps.github.set_status(target, issue, target.status_ready_option_id)
    if "auto" not in row.get("labels", []):
        deps.github.add_label(target, issue, "auto")
    deps.notifier.send("status", lines=[
        f"#{issue} enqueued at the head (boost {NEXT_BOOST})"])


def _handle_telegram(cfg: Config, deps: Deps, dry_run: bool = False) -> None:
    if dry_run:
        return
    tasks = load_all(cfg.state_dir)
    human_parked = [t for t in tasks if t.park == PARK_HUMAN]
    for ev in inbound.fetch_events(cfg.state_dir):
        if isinstance(ev, Command) and ev.name == "status":
            deps.notifier.send("status", lines=_status_lines(cfg))
        elif isinstance(ev, Command) and ev.name == "attach":
            match = [t for t in tasks if t.issue == ev.issue and t.park]
            if match:
                _wake(cfg, match[0], "", hold=True)
            else:
                deps.notifier.send("status",
                                   lines=[f"#{ev.issue} is not parked"])
        elif isinstance(ev, Command) and ev.name == "queue":
            try:
                deps.notifier.send("queue", lines=_queue_lines(cfg, deps))
            except (subprocess.CalledProcessError, OSError,
                    LookupError, ValueError) as exc:
                deps.notifier.send("status", lines=[f"/queue failed: {exc}"])
        elif isinstance(ev, Command) and ev.name == "boost":
            try:
                _handle_boost(cfg, deps, ev.issue, ev.amount)
            except (subprocess.CalledProcessError, OSError,
                    LookupError, ValueError) as exc:
                deps.notifier.send("status",
                                   lines=[f"#{ev.issue} boost failed: {exc}"])
        elif isinstance(ev, Command) and ev.name == "next":
            try:
                _handle_next(cfg, deps, ev.issue, ev.force)
            except (subprocess.CalledProcessError, OSError,
                    LookupError, ValueError) as exc:
                deps.notifier.send("status",
                                   lines=[f"#{ev.issue} next failed: {exc}"])
        elif isinstance(ev, Reply):
            match = [t for t in human_parked if t.park_msg_id == ev.reply_to_msg_id]
            if match:
                _wake(cfg, match[0], ev.text)
            else:
                deps.notifier.send("status",
                                   lines=["(reply didn't match any parked task)"])
        elif isinstance(ev, Plain):
            if len(human_parked) == 1:
                _wake(cfg, human_parked[0], ev.text)
            else:
                deps.notifier.send("status", lines=(
                    ["Which task? Reply directly to its parked message:"]
                    + [f"#{t.issue} {t.title}" for t in human_parked]))


def _notify(deps: Deps, target: Target, task: TaskState, template: str,
            note: str = "") -> None:
    deps.notifier.send(template, issue=task.issue, title=task.title,
                       url=_url(target, task.issue), note=note)


def _spawn_stage(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 stage: Stage, spec_path: str = "") -> TaskState:
    ctx = dict(
        issue_number=task.issue, issue_title=task.title,
        issue_url=_url(target, task.issue), repo=target.repo,
        branch=task.branch, slot=task.slot,
        backend_port=8100 + task.slot, frontend_port=5200 + task.slot,
        verify_cmd=target.verify_cmd.format(slot=task.slot),
        spec_path=spec_path,
    )
    prompt = render_stage_prompt(stage, ctx)
    # Reset the signal BEFORE spawning, or the next pass re-reads the
    # previous stage's `done` and advances again.
    agent_dir = Path(task.worktree) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stage.json").write_text(json.dumps(
        {"stage": stage.value, "status": "working"}))
    deps.sessions.spawn_stage(task.issue, task.worktree, prompt, stage.value)
    task = replace(task, stage=stage, updated_at=_now())
    save(cfg.state_dir, task)
    return task


def _budget_edge(cfg: Config, deps: Deps, budget_ok: bool, note: str) -> None:
    """Edge-triggered stall/resume pings via a marker file."""
    marker = Path(cfg.state_dir) / "budget-stalled"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if not budget_ok and not marker.exists():
        marker.write_text(_now())
        deps.notifier.send("budget_stall", issue=0, title="(all tasks)",
                           url="", note=note)
    elif budget_ok and marker.exists():
        marker.unlink()
        deps.notifier.send("budget_resume", issue=0, title="(all tasks)",
                           url="", note=note)


def _park_for_input(cfg: Config, deps: Deps, target: Target, task: TaskState,
                    note: str) -> None:
    tail = deps.sessions.capture_tail(task.issue)
    msg_id = deps.notifier.send(
        "parked_question", issue=task.issue, title=task.title,
        url=_url(target, task.issue),
        note=(note + ("\n\n" + tail if tail else "")).strip() or "(no detail)")
    deps.sessions.end(task.issue)
    clear_waiting(cfg.state_dir, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_HUMAN, park_msg_id=msg_id,
                                updated_at=_now()))


def _park_for_ci(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 run_id: int) -> None:
    deps.sessions.end(task.issue)
    clear_waiting(cfg.state_dir, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_CI, ci_run_id=run_id,
                                updated_at=_now()))


def _wake_ci(cfg: Config, deps: Deps, target: Target) -> None:
    for task in load_all(cfg.state_dir):
        if task.target != target.name or task.park != PARK_CI:
            continue
        try:
            conclusion = deps.github.run_status(target, task.ci_run_id)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[warn] run_status failed for #{task.issue} run {task.ci_run_id}: {exc}",
                  file=sys.stderr)
            continue
        if not conclusion:
            continue
        reply = (f"E2E run {task.ci_run_id} concluded: {conclusion} — "
                 f"fetch logs with: gh run view {task.ci_run_id} --log-failed")
        save(cfg.state_dir, replace(task, park=PARK_WAKE, pending_reply=reply,
                                    ci_run_id=0, updated_at=_now()))


def _resume_woken(cfg: Config, deps: Deps, target: Target,
                  budget_ok: bool) -> None:
    if not budget_ok:
        return
    woken = sorted(
        [t for t in load_all(cfg.state_dir)
         if t.target == target.name and t.park == PARK_WAKE],
        key=lambda t: t.updated_at,
    )
    for task in woken:
        tasks = [t for t in load_all(cfg.state_dir) if t.target == target.name]
        if len(active(tasks)) >= cfg.capacity:
            return
        agent_dir = Path(task.worktree) / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        # Rewrite stage.json BEFORE resuming, or the next pass re-reads
        # blocked/awaiting-ci and re-parks the freshly resumed session.
        (agent_dir / "stage.json").write_text(json.dumps(
            {"stage": task.stage.value, "status": "working"}))
        if task.hold_for_attach:
            deps.sessions.resume(task.issue, task.worktree,
                                 "The operator is attaching to talk to you "
                                 "directly. Wait for their input.")
            deps.notifier.send("resumed_for_attach", issue=task.issue,
                               title=task.title, url=_url(target, task.issue),
                               note="")
        else:
            deps.sessions.resume(task.issue, task.worktree,
                                 task.pending_reply or "Continue.")
        save(cfg.state_dir, replace(task, park="", pending_reply="",
                                    hold_for_attach=False, park_msg_id=0,
                                    updated_at=_now()))


def _drive_task(cfg: Config, deps: Deps, target: Target, task: TaskState,
                budget_ok: bool) -> None:
    signal = read_stage_signal(task.worktree)
    alive = deps.sessions.is_alive(task.issue)
    waiting = has_waiting(cfg.state_dir, task.issue)
    for act in next_actions(task, signal, alive, waiting=waiting):
        if isinstance(act, NoOp):
            continue
        if isinstance(act, ParkForInput):
            _park_for_input(cfg, deps, target, task, act.note)
            return
        if isinstance(act, ParkForCI):
            _park_for_ci(cfg, deps, target, task, act.run_id)
            return
        if isinstance(act, SetTaskStage):
            clear_waiting(cfg.state_dir, task.issue)
            task = replace(task, stage=act.stage, updated_at=_now())
            save(cfg.state_dir, task)
        elif isinstance(act, Notify):
            _notify(deps, target, task, act.template, act.note)
        elif isinstance(act, SpawnStage):
            if not budget_ok:
                return  # signal persists; retried next pass
            clear_waiting(cfg.state_dir, task.issue)
            spec_path = signal.artifact if act.stage is Stage.PLAN else ""
            task = _spawn_stage(cfg, deps, target, task, act.stage, spec_path)
        elif isinstance(act, HandleCrash):
            _notify(deps, target, task, "session_crashed")
            deps.github.release(target, task.issue, "session crashed mid-stage")
            save(cfg.state_dir, replace(task, stage=Stage.FAILED,
                                        updated_at=_now()))


def _claim_new(cfg: Config, deps: Deps, target: Target,
               dry_run: bool) -> None:
    tasks = [t for t in load_all(cfg.state_dir) if t.target == target.name]
    free = cfg.capacity - len(active(tasks))
    if free <= 0:
        return
    known = {t.issue for t in tasks}
    for cand in deps.github.candidates(target):
        if free <= 0:
            break
        if cand.number in known:
            continue
        slot = allocate_slot(load_all(cfg.state_dir))
        if slot is None:
            break
        wt = create_workspace(target, cand.number, dry_run=dry_run)  # if this throws: board never claimed (still Ready), no state file → naturally retried next pass, no strand
        task = TaskState(issue=cand.number, target=target.name,
                         stage=Stage.QUEUED, slot=slot, worktree=wt,
                         branch=f"agent/task-{cand.number}",
                         title=cand.title, updated_at=_now())
        save(cfg.state_dir, task)  # state exists BEFORE the irreversible claim, so a partial claim is recoverable
        try:
            deps.github.claim(target, cand)  # irreversible board mutation — last
            _spawn_stage(cfg, deps, target, task, Stage.SPEC)
        except Exception:
            deps.github.release(target, cand.number, "claim/spawn failed after provisioning")
            raise
        free -= 1


def run_pass(cfg: Config, deps: Deps, dry_run: bool = False) -> None:
    _handle_telegram(cfg, deps, dry_run)
    usage = fetch_usage(cfg.state_dir)
    budget_ok = should_spawn(usage, cfg.budget_threshold,
                             cfg.racing_minutes, cfg.racing_threshold)
    _budget_edge(cfg, deps, budget_ok,
                 note=f"{usage.source}: {usage.utilization:.0%}, "
                      f"reset in {usage.minutes_to_reset:.0f}m")
    for target in cfg.targets:
        for task in [t for t in load_all(cfg.state_dir)
                     if t.target == target.name and not t.park
                     and t.stage in IN_FLIGHT_STAGES]:
            _drive_task(cfg, deps, target, task, budget_ok)
        _wake_ci(cfg, deps, target)
        _resume_woken(cfg, deps, target, budget_ok)
        if budget_ok:
            _claim_new(cfg, deps, target, dry_run)


def send_digest(cfg: Config, deps: Deps) -> None:
    deps.notifier.send("daily_digest", lines=_status_lines(cfg))


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-ops-dispatcher")
    ap.add_argument("--config", default="targets.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--digest", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    deps = Deps(github=GitHubClient(dry_run=args.dry_run),
                sessions=Sessions(dry_run=args.dry_run, memory=cfg.session_memory, cpus=cfg.session_cpus),
                notifier=Notifier(dry_run=args.dry_run))
    if args.digest:
        send_digest(cfg, deps)
    else:
        with pass_lock(cfg.state_dir):
            run_pass(cfg, deps, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
