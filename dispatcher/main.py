"""One dispatcher pass: candidates → capacity → budget → claim → drive.

Stateless per pass — the view is rebuilt every time from the board (via
candidates), state files, stage.json signals, and tmux liveness. Run by a
systemd timer; one invocation = one pass."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from dispatcher.budget import fetch_usage, should_spawn
from dispatcher.config import Config, Target, load_config
from dispatcher.github import GitHubClient
from dispatcher.machine import (HandleCrash, NoOp, Notify, SetTaskStage,
                                SpawnStage, next_actions)
from dispatcher.prompts import render_stage_prompt
from dispatcher.sessions import Sessions
from dispatcher.state import (IN_FLIGHT_STAGES, Stage, TaskState,
                              allocate_slot, load_all, read_stage_signal, save)
from dispatcher.workspace import create_workspace
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


def _drive_task(cfg: Config, deps: Deps, target: Target, task: TaskState,
                budget_ok: bool) -> None:
    signal = read_stage_signal(task.worktree)
    alive = deps.sessions.is_alive(task.issue)
    for act in next_actions(task, signal, alive):
        if isinstance(act, NoOp):
            continue
        if isinstance(act, SetTaskStage):
            task = replace(task, stage=act.stage, updated_at=_now())
            save(cfg.state_dir, task)
        elif isinstance(act, Notify):
            _notify(deps, target, task, act.template, act.note)
        elif isinstance(act, SpawnStage):
            if not budget_ok:
                return  # signal persists; retried next pass
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
    in_flight = [t for t in tasks if t.stage in IN_FLIGHT_STAGES]
    free = cfg.capacity - len(in_flight)
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
    usage = fetch_usage(cfg.state_dir)
    budget_ok = should_spawn(usage, cfg.budget_threshold,
                             cfg.racing_minutes, cfg.racing_threshold)
    _budget_edge(cfg, deps, budget_ok,
                 note=f"{usage.source}: {usage.utilization:.0%}, "
                      f"reset in {usage.minutes_to_reset:.0f}m")
    for target in cfg.targets:
        for task in load_all(cfg.state_dir):
            if task.target == target.name and task.stage in IN_FLIGHT_STAGES:
                _drive_task(cfg, deps, target, task, budget_ok)
        if budget_ok:
            _claim_new(cfg, deps, target, dry_run)


def send_digest(cfg: Config, deps: Deps) -> None:
    lines = [f"#{t.issue} {t.title} — {t.stage.value} (slot {t.slot})"
             for t in load_all(cfg.state_dir) if t.stage in IN_FLIGHT_STAGES]
    deps.notifier.send("daily_digest", lines=lines or ["(nothing in flight)"])


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-ops-dispatcher")
    ap.add_argument("--config", default="targets.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--digest", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    deps = Deps(github=GitHubClient(dry_run=args.dry_run),
                sessions=Sessions(dry_run=args.dry_run),
                notifier=Notifier(dry_run=args.dry_run))
    if args.digest:
        send_digest(cfg, deps)
    else:
        run_pass(cfg, deps, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
