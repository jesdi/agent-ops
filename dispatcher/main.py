"""One dispatcher pass: candidates → capacity → budget → claim → drive.

Stateless per pass — the view is rebuilt every time from the board (via
candidates), state files, stage.json signals, and tmux liveness. Run by a
systemd timer; one invocation = one pass."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from dispatcher.budget import fetch_usage, should_spawn
from dispatcher.convergence import pass_lock
from dispatcher.config import Config, Target, load_config, policy_for
from dispatcher import eventlog, failures, intents, pr_poll, queue_ops, relogin
from dispatcher.github import GitHubClient
from dispatcher.machine import (HandleCrash, NoOp, Notify, ParkForCI,
                                ParkForInput, ParkForReview, RetryStage,
                                SetTaskStage, SpawnStage, next_actions)
from dispatcher.models import resolve
from dispatcher.prompts import render_stage_prompt
from dispatcher.sessions import Sessions
from dispatcher.state import (IN_FLIGHT_STAGES, NO_SLOT, PARK_CI, PARK_HUMAN,
                              PARK_LOGIN, PARK_REVIEW, PARK_WAKE,
                              Stage, TaskState, active, allocate_slot,
                              clear_waiting, delete, has_attached, has_waiting,
                              load, load_all, read_stage_signal, save)
from dispatcher.workspace import create_workspace, remove_workspace
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


# Stages that aren't policy stages but hold a live session from one: a
# claimed task is about to spawn spec, and a task at the spec-review gate
# still has its spec session running. Stage.BLOCKED and
# Stage.STALLED_ON_BUDGET genuinely lose the originating stage in
# TaskState (nothing in machine.py sets it), so they deliberately fall
# through to stage.value, which matches no `use:` key and lands on the
# policy default.
_POLICY_STAGE = {Stage.QUEUED: "spec", Stage.AWAITING_SPEC_REVIEW: "spec"}


def _model_for(cfg: Config, target: Target | None, task: TaskState,
               stage: Stage) -> str:
    """target is None for a task whose target has left the config — it still
    resolves, against the global policy, since there is no per-target one to
    look up. Every model resolution goes through here, so the stage mapping
    above is applied exactly once."""
    policy = policy_for(cfg, target) if target else cfg.models
    return resolve(policy, _POLICY_STAGE.get(stage, stage.value),
                   task.effort, task.labels)


def _log_model(worktree: str, stage: Stage, model: str) -> None:
    """Durable per-worktree breadcrumb. stage.json is co-owned — sessions
    overwrite it when they signal — so the log is the record that survives."""
    p = Path(worktree) / ".agent" / "models.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(f"{_now()} {stage.value} {model}\n")


def _url(target: Target, issue: int) -> str:
    return f"https://github.com/{target.repo}/issues/{issue}"


def _wake(cfg: Config, task: TaskState, text: str, hold: bool = False) -> None:
    save(cfg.state_dir, replace(task, park=PARK_WAKE, pending_reply=text,
                                hold_for_attach=hold, updated_at=_now()))


def _inject_login_code(cfg: Config, deps: Deps, task: TaskState,
                       code: str) -> None:
    """Reply to a login park = the OAuth authorization code. Raw keystrokes
    into the still-alive pane — NOT the wake/resume path, which would spawn
    `claude --continue` over the live login prompt. Park cleared; stage
    signals, the Stop hook, and the stall timer take over. A wrong code
    leaves the screen static and the stall detector simply re-fires.

    The reply may arrive hours later, so the prompt is re-verified first: the
    tmux session is a HOST one (is_alive only means `has-session`), and once
    claude has exited the pane is back at the host shell, where send_text
    would execute the operator's text as a shell command outside the sandbox.

    Un-parking then restores the same invariant _resume_woken and _retry_plan
    enforce — no waiting marker, a `working` signal — or the very next pass
    re-reads blocked/awaiting-ci, re-parks, and ENDS the session that was
    just re-authenticated."""
    if relogin.classify_login(deps.sessions.capture_tail(task.issue)) is None:
        deps.notifier.send("status", lines=[
            f"#{task.issue} is no longer at a login prompt — code NOT typed "
            f"(the pane would have run it as a shell command). Still parked; "
            f"attach to the session to sort it out."])
        return
    deps.sessions.send_text(task.issue, code.strip())
    clear_waiting(cfg.state_dir, task.issue)
    signal = read_stage_signal(task.worktree)
    if signal is not None and signal.status != "working":
        by_name = {t.name: t for t in cfg.targets}
        model = _model_for(cfg, by_name.get(task.target), task, task.stage)
        agent_dir = Path(task.worktree) / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "stage.json").write_text(json.dumps(
            {"stage": task.stage.value, "status": "working", "model": model}))
    save(cfg.state_dir, replace(task, park="", park_msg_id=0,
                                park_note="", updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "login-code-injected",
                          target=task.target, issue=task.issue,
                          stage=task.stage.value, detail="login code injected")


def _status_lines(cfg: Config) -> list[str]:
    by_name = {t.name: t for t in cfg.targets}
    tasks = [t for t in load_all(cfg.state_dir) if t.stage in IN_FLIGHT_STAGES]
    lines = []
    for t in tasks:
        model = _model_for(cfg, by_name.get(t.target), t, t.stage)
        slot = "(no slot)" if t.slot == NO_SLOT else f"(slot {t.slot})"
        lines.append(f"#{t.issue} {t.title} — {t.stage.value} [{model}]"
                     + (f" [{t.park}]" if t.park else "") + f" {slot}")
    lines = lines or ["(nothing in flight)"]
    lines.append(f"capacity {len(active(tasks))}/{cfg.capacity}")
    return lines


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
    plan = queue_ops.plan_boost(row, amount)
    queue_ops.apply_plan(deps.github, target, issue, plan)
    deps.notifier.send("status", lines=plan.reason.split("\n"))


def _handle_next(cfg: Config, deps: Deps, issue: int, force: bool) -> None:
    located = _locate(cfg, deps, issue)
    if not located:
        return
    target, row = located
    plan = queue_ops.plan_next(row, force)
    queue_ops.apply_plan(deps.github, target, issue, plan)
    deps.notifier.send("status", lines=plan.reason.split("\n"))


def _handle_telegram(cfg: Config, deps: Deps, dry_run: bool = False) -> None:
    if dry_run:
        return
    tasks = load_all(cfg.state_dir)
    # Both parks answer the same way — a reply is text for the session. They
    # stay distinct kinds so /status and the board can say "needs input
    # mid-stage" vs "spec ready, review at leisure".
    reply_parked = [t for t in tasks if t.park in (PARK_HUMAN, PARK_REVIEW)]
    login_parked = [t for t in tasks if t.park == PARK_LOGIN]
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
            login = [t for t in login_parked
                     if t.park_msg_id == ev.reply_to_msg_id]
            match = [t for t in reply_parked
                     if t.park_msg_id == ev.reply_to_msg_id]
            if login:
                _inject_login_code(cfg, deps, login[0], ev.text)
            elif match:
                _wake(cfg, match[0], ev.text)
            else:
                deps.notifier.send("status",
                                   lines=["(reply didn't match any parked task)"])
        elif isinstance(ev, Plain):
            if len(reply_parked) == 1:
                _wake(cfg, reply_parked[0], ev.text)
            else:
                deps.notifier.send("status", lines=(
                    ["Which task? Reply directly to its parked message:"]
                    + [f"#{t.issue} {t.title}" for t in reply_parked]))


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
    model = _model_for(cfg, target, task, stage)
    # Reset the signal BEFORE spawning, or the next pass re-reads the
    # previous stage's `done` and advances again.
    agent_dir = Path(task.worktree) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stage.json").write_text(json.dumps(
        {"stage": stage.value, "status": "working", "model": model}))
    _log_model(task.worktree, stage, model)
    deps.sessions.spawn_stage(task.issue, task.worktree, prompt, stage.value, model)
    task = replace(task, stage=stage, artifact="", updated_at=_now())
    save(cfg.state_dir, task)
    eventlog.append_event(cfg.state_dir, "stage-started", target=target.name,
                          issue=task.issue, stage=stage.value, model=model)
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
    login = relogin.classify_login(tail)
    if login is not None and _park_for_login(cfg, deps, target, task, note,
                                             tail, login):
        return
    msg_id = deps.notifier.send(
        "parked_question", issue=task.issue, title=task.title,
        url=_url(target, task.issue),
        note=(note + ("\n\n" + tail if tail else "")).strip() or "(no detail)")
    deps.sessions.end(task.issue)
    clear_waiting(cfg.state_dir, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_HUMAN, park_msg_id=msg_id,
                                park_note=note, updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, stage=task.stage.value, detail=note)


def _park_for_login(cfg: Config, deps: Deps, target: Target, task: TaskState,
                    note: str, tail: str, login: relogin.LoginPrompt) -> bool:
    """Unlike every other park, the session is NOT ended: the reply path
    types the OAuth code into this exact pane. Only this task is parked;
    other sessions hitting the shared-claude-home prompt are caught by
    their own stall timers.

    Returns False when the ping could not be delivered (Notifier.send yields
    0 on any send error). A park_msg_id of 0 matches no reply, so the task
    would hold a live container forever with no way back in — the caller
    falls through to the generic park, which at least releases it.

    The waiting marker is cleared like in every other park: it says "the
    session stopped mid-stage", and leaving it set makes the pass after the
    un-park re-park (and END) the freshly re-authenticated session. Pane
    liveness is what this park protects, and the marker has no part in it."""
    msg_id = deps.notifier.send(
        "needs_relogin", issue=task.issue, title=task.title,
        url=_url(target, task.issue),
        login_url=login.url or "(attach to the session to see the URL)",
        note=(note + ("\n\n" + tail if tail else "")).strip() or "(no detail)")
    if msg_id == 0:
        return False
    clear_waiting(cfg.state_dir, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_LOGIN, park_msg_id=msg_id,
                                park_note=note, updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail="needs re-login: " + note)
    return True


def _retry_plan(cfg: Config, deps: Deps, target: Target, task: TaskState,
                reason: str) -> None:
    """Resume the plan session with the format-check failure, in place, rather
    than failing the task. --continue reads the transcript from claude-home, so
    context survives ending the (zombie) session first — which we must do, or
    _launch would type `claude --continue` INTO the stopped claude's input box
    (same failure mode as spawning over a live session)."""
    model = _model_for(cfg, target, task, Stage.PLAN)
    agent_dir = Path(task.worktree) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Rewrite the signal to working BEFORE resuming, or the next pass re-reads
    # `done`, re-checks the still-unfixed plan, and burns the retry immediately.
    (agent_dir / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "working", "model": model}))
    _log_model(task.worktree, Stage.PLAN, model)
    clear_waiting(cfg.state_dir, task.issue)
    deps.sessions.end(task.issue)
    deps.sessions.resume(
        task.issue, task.worktree,
        f"Your .agent/plan.md failed the pipeline's mechanical format check: "
        f"{reason}. Fix it in place — every task needs an H3 heading of exactly "
        f"`### Task N:` (not `## Task N:`) and the plan must contain a "
        f"`**Goal:**` line — then re-write .agent/stage.json with "
        f'status "done". Do not re-plan from scratch; only fix the formatting.',
        model)
    save(cfg.state_dir, replace(task, plan_retries=task.plan_retries + 1,
                                updated_at=_now()))
    _notify(deps, target, task, "plan_retry", reason)


def _park_for_ci(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 run_id: int) -> None:
    deps.sessions.end(task.issue)
    clear_waiting(cfg.state_dir, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_CI, ci_run_id=run_id,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail=f"awaiting CI run {run_id}")


def _grace_elapsed(cfg: Config, task: TaskState) -> bool:
    """Has the spec-review gate gone unanswered past the grace period?
    `updated_at` was stamped when the stage flipped to AWAITING_SPEC_REVIEW,
    so no extra timer field is needed and the pass stays stateless. An
    unparseable timestamp never expires — failing closed keeps a corrupt state
    file from parking the whole queue."""
    try:
        since = datetime.fromisoformat(task.updated_at)
    except (TypeError, ValueError):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - since).total_seconds()
    return age >= cfg.spec_review_grace_minutes * 60


def _park_for_review(cfg: Config, deps: Deps, target: Target,
                     task: TaskState) -> None:
    """Park a finished spec for a human to read whenever they wake up. The
    only park that also releases the SLOT: the spec stage never used the
    slot's ports and worktrees are per-issue, so resume can take any free
    slot — and freeing it is the whole point, since a held slot would cap the
    overnight run at MAX_SLOTS specs."""
    tail = deps.sessions.capture_tail(task.issue)
    # No msg_id == 0 guard here (unlike _park_for_login): if the ping fails,
    # the task is not stranded — the session is ended and the operator can still
    # reach it via /attach, the console `resume` intent, or plain-text wakes.
    # This matches the same reasoning in _park_for_input.
    msg_id = deps.notifier.send(
        "spec_parked", issue=task.issue, title=task.title,
        url=_url(target, task.issue), note=tail.strip() or "(no detail)")
    deps.sessions.end(task.issue)
    clear_waiting(cfg.state_dir, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_REVIEW, park_msg_id=msg_id,
                                park_note="spec ready for review",
                                slot=NO_SLOT, updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail="spec review grace expired")


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


def _poll_prs(cfg: Config, deps: Deps, target: Target) -> None:
    """Watch every pr-open task's PR — unconditionally: one gh read per
    task, no capacity involved. Reaction (address-review spawn) is gated
    separately in _spawn_feedback."""
    for task in load_all(cfg.state_dir):
        if task.target != target.name or task.stage is not Stage.PR_OPEN:
            continue
        try:
            if not task.pr_number:
                n = deps.github.pr_number_for_branch(target, task.branch)
                if not n:
                    print(f"[warn] #{task.issue}: no PR found for branch "
                          f"{task.branch}", file=sys.stderr)
                    continue
                task = replace(task, pr_number=n, updated_at=_now())
                save(cfg.state_dir, task)
            res = pr_poll.classify(deps.github.pr_view(target, task.pr_number),
                                   task.feedback_cursor,
                                   deps.github.viewer_login())
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[warn] PR poll failed for #{task.issue}: {exc}",
                  file=sys.stderr)
            continue
        if res.kind == "merged":
            _finish_merged(cfg, deps, target, task)
        elif res.kind == "closed":
            save(cfg.state_dir, replace(task, stage=Stage.FAILED,
                                        updated_at=_now()))
            eventlog.append_event(cfg.state_dir, "pr-closed",
                                  target=target.name, issue=task.issue,
                                  stage=Stage.PR_OPEN.value,
                                  detail=f"PR #{task.pr_number} closed unmerged")
            _notify(deps, target, task, "pr_closed",
                    f"PR #{task.pr_number}. Worktree preserved for autopsy.")
        elif res.kind == "feedback" and not task.feedback_pending:
            save(cfg.state_dir, replace(task, feedback_pending=True,
                                        updated_at=_now()))
            eventlog.append_event(cfg.state_dir, "pr-feedback",
                                  target=target.name, issue=task.issue,
                                  stage=Stage.PR_OPEN.value,
                                  detail=f"newest {res.latest_ts}")
            _notify(deps, target, task, "pr_feedback",
                    f"PR #{task.pr_number}, newest feedback {res.latest_ts}")


def _finish_merged(cfg: Config, deps: Deps, target: Target,
                   task: TaskState) -> None:
    """Ordered so a mid-sequence gh failure retries next pass (a merged PR
    still reads merged): board first (the raising step), then teardown
    (best-effort by construction), then the state flip that stops polling."""
    if target.status_done_option_id:
        deps.github.set_status(target, task.issue, target.status_done_option_id)
    else:
        print(f"[warn] {target.name}: status_done_option_id unset — board "
              f"not updated for #{task.issue}", file=sys.stderr)
    deps.sessions.end(task.issue)
    remove_workspace(target, task.worktree, task.branch)
    deps.github.delete_branch(target, task.branch)
    save(cfg.state_dir, replace(task, stage=Stage.DONE, park="",
                                feedback_pending=False, done_at=_now(),
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "merged", target=target.name,
                          issue=task.issue, stage=Stage.DONE.value,
                          detail=f"PR #{task.pr_number}")
    _notify(deps, target, task, "task_done",
            f"https://github.com/{target.repo}/pull/{task.pr_number}")


def _flush_done(cfg: Config) -> None:
    cutoff = cfg.done_retention_days * 86400
    for task in load_all(cfg.state_dir):
        if task.stage is not Stage.DONE or not task.done_at:
            continue
        try:
            since = datetime.fromisoformat(task.done_at)
        except (TypeError, ValueError):
            continue  # unparseable never expires — fail closed, card stays
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - since).total_seconds() >= cutoff:
            delete(cfg.state_dir, task.issue)
            eventlog.append_event(cfg.state_dir, "flushed",
                                  target=task.target, issue=task.issue,
                                  stage=Stage.DONE.value)


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
        if has_attached(cfg.state_dir, task.issue):
            continue  # a human is typing in this session — do not resume over them
        tasks = [t for t in load_all(cfg.state_dir) if t.target == target.name]
        if len(active(tasks)) >= cfg.capacity:
            return
        if task.slot == NO_SLOT:
            # Gate-parked tasks gave their slot back. Take any free one —
            # worktrees are per-issue and the spec stage never bound the
            # slot's ports, so the number need not be the original.
            slot = allocate_slot(load_all(cfg.state_dir))
            if slot is None:
                continue  # no free slot: stays parked, retried next pass
            task = replace(task, slot=slot)
        model = _model_for(cfg, target, task, task.stage)
        agent_dir = Path(task.worktree) / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        # Rewrite stage.json BEFORE resuming, or the next pass re-reads
        # blocked/awaiting-ci and re-parks the freshly resumed session.
        (agent_dir / "stage.json").write_text(json.dumps(
            {"stage": task.stage.value, "status": "working", "model": model}))
        _log_model(task.worktree, task.stage, model)
        # End first, unconditionally. Most parks already stopped the session,
        # but /attach on a PARK_LOGIN task reaches here with the pane still
        # LIVE, and _launch would then type the podman command INTO the
        # running claude (the failure _retry_plan and SpawnStage guard).
        deps.sessions.end(task.issue)
        if task.hold_for_attach:
            deps.sessions.resume(task.issue, task.worktree,
                                 "The operator is attaching to talk to you "
                                 "directly. Wait for their input.", model)
            deps.notifier.send("resumed_for_attach", issue=task.issue,
                               title=task.title, url=_url(target, task.issue),
                               note="")
        else:
            deps.sessions.resume(task.issue, task.worktree,
                                 task.pending_reply or "Continue.", model)
        save(cfg.state_dir, replace(task, park="", pending_reply="",
                                    hold_for_attach=False, park_msg_id=0,
                                    park_note="", updated_at=_now()))
        eventlog.append_event(cfg.state_dir, "resumed", target=target.name,
                              issue=task.issue, stage=task.stage.value,
                              model=model)


def _drive_task(cfg: Config, deps: Deps, target: Target, task: TaskState,
                budget_ok: bool, dry_run: bool = False) -> None:
    if has_attached(cfg.state_dir, task.issue):
        return  # held for a live human attach: no park, no reap, no spawn
    signal = read_stage_signal(task.worktree)
    alive = deps.sessions.is_alive(task.issue)
    waiting = has_waiting(cfg.state_dir, task.issue)
    # Query tmux idle only when it can matter: detection enabled and the
    # session alive (the crash path owns dead sessions).
    idle = (deps.sessions.idle_seconds(task.issue)
            if alive and cfg.stall_after_seconds > 0 else None)
    for act in next_actions(task, signal, alive, waiting=waiting,
                            idle_seconds=idle,
                            stall_after=cfg.stall_after_seconds,
                            grace_elapsed=_grace_elapsed(cfg, task)):
        if isinstance(act, NoOp):
            continue
        if isinstance(act, ParkForInput):
            _park_for_input(cfg, deps, target, task, act.note)
            return
        if isinstance(act, ParkForReview):
            _park_for_review(cfg, deps, target, task)
            return
        if isinstance(act, ParkForCI):
            _park_for_ci(cfg, deps, target, task, act.run_id)
            return
        if isinstance(act, RetryStage):
            if not budget_ok:
                return  # signal persists; retried next pass once budget clears
            _retry_plan(cfg, deps, target, task, act.reason)
            return
        if isinstance(act, SetTaskStage):
            clear_waiting(cfg.state_dir, task.issue)
            extra: dict = {}
            if act.stage is Stage.PR_OPEN and signal is not None:
                m = re.search(r"/pull/(\d+)", signal.artifact or signal.note or "")
                if m:
                    extra["pr_number"] = int(m.group(1))
            task = replace(task, stage=act.stage,
                           artifact=act.artifact or task.artifact,
                           updated_at=_now(), **extra)
            save(cfg.state_dir, task)
            if act.stage is Stage.PR_OPEN:
                eventlog.append_event(cfg.state_dir, "pr-opened",
                                      target=target.name, issue=task.issue,
                                      stage=act.stage.value)
        elif isinstance(act, Notify):
            _notify(deps, target, task, act.template, act.note)
        elif isinstance(act, SpawnStage):
            if not budget_ok:
                return  # signal persists; retried next pass
            clear_waiting(cfg.state_dir, task.issue)
            # The previous stage's claude is usually still alive here — an
            # interactive session cannot exit itself. _launch would type
            # the next stage's podman command INTO it (and the container
            # name would collide). End it first; no-op when already dead.
            deps.sessions.end(task.issue)
            spec_path = signal.artifact if act.stage is Stage.PLAN else ""
            task = _spawn_stage(cfg, deps, target, task, act.stage, spec_path)
        elif isinstance(act, HandleCrash):
            _notify(deps, target, task, "session_crashed")
            deps.github.release(target, task.issue, "session crashed mid-stage")
            save(cfg.state_dir, replace(task, stage=Stage.FAILED,
                                        updated_at=_now()))
            eventlog.append_event(cfg.state_dir, "failed", target=target.name,
                                  issue=task.issue, stage=task.stage.value,
                                  detail="session crashed mid-stage")
            _report_session_crash(cfg, deps, target, task, dry_run)


def _report_session_crash(cfg: Config, deps: Deps, target: Target,
                          task: TaskState, dry_run: bool) -> None:
    rep = failures.FailureReport(
        klass="session-crash", target=target.name, issue=task.issue,
        title=f"session crashed during {task.stage.value}: {task.title}",
        error=(f"tmux session task-{task.issue} died during stage "
               f"{task.stage.value}"),
        log_tail=deps.sessions.capture_tail(task.issue, lines=30),
        repro=f"cd {task.worktree} && claude --continue  # inside session image",
        worktree=task.worktree)
    blocker = failures.report_failure(cfg, deps, rep, dry_run=dry_run)
    if blocker:
        try:
            deps.github.append_blocked_by(target, task.issue, blocker)
        except Exception as exc:
            print(f"[warn] append_blocked_by failed for #{task.issue}: {exc}",
                  file=sys.stderr)


def _report_provisioning_failure(cfg: Config, deps: Deps, target: Target,
                                 cand, dry_run: bool) -> None:
    wt = str(Path(target.worktrees_path) / f"task-{cand.number}")
    rep = failures.FailureReport(
        klass="provisioning", target=target.name, issue=cand.number,
        title=f"provisioning failed: {cand.title}",
        error=traceback.format_exc(),
        log_tail=failures.setup_log_tail(wt),
        repro=f"podman run --rm -v {wt}:{wt} -w {wt} agent-ops-session "
              f"{target.setup_cmd}",
        worktree=wt)
    blocker = failures.report_failure(cfg, deps, rep, dry_run=dry_run)
    # Quarantine only once the report exists (marker written) — a gh outage
    # leaves neither, so the next pass retries both. blocker may still be 0
    # when infra_repo is unset; that record blocks until manually deleted.
    if not dry_run and failures.reported(cfg.state_dir, rep):
        failures.write_quarantine(cfg.state_dir, target.name, cand.number,
                                  blocker_repo=cfg.infra_repo if blocker else "",
                                  blocker_issue=blocker,
                                  fp=failures.fingerprint(rep))


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
        if failures.check_quarantine(cfg.state_dir, deps.github, target.name,
                                     cand.number):
            continue
        slot = allocate_slot(load_all(cfg.state_dir))
        if slot is None:
            break
        try:
            wt = create_workspace(target, cand.number, dry_run=dry_run)
        except Exception:
            # Board never claimed (claim is last, still Ready) → no release
            # needed; report + quarantine, and the pass survives.
            _report_provisioning_failure(cfg, deps, target, cand, dry_run)
            # Cap at one provisioning failure per target per pass: a
            # systemic fault (git remote down, podman down, worktrees
            # volume full) would otherwise fail EVERY remaining Ready
            # candidate in this loop, filing an issue + ping + quarantine
            # record per candidate. A systemic cause is far likelier than a
            # per-candidate one, so stop here — the next pass retries the
            # remaining candidates. This `break` only exits this target's
            # candidate loop; run_pass still processes other targets.
            break
        task = TaskState(issue=cand.number, target=target.name,
                         stage=Stage.QUEUED, slot=slot, worktree=wt,
                         branch=f"agent/task-{cand.number}",
                         title=cand.title, updated_at=_now(),
                         effort=cand.effort, labels=cand.labels)
        save(cfg.state_dir, task)  # state exists BEFORE the irreversible claim, so a partial claim is recoverable
        eventlog.append_event(cfg.state_dir, "claimed", target=target.name,
                              issue=cand.number, stage=Stage.QUEUED.value)
        try:
            deps.github.claim(target, cand)  # irreversible board mutation — last
            _spawn_stage(cfg, deps, target, task, Stage.SPEC)
        except Exception:
            deps.github.release(target, cand.number, "claim/spawn failed after provisioning")
            raise
        free -= 1


def _apply_one_intent(cfg: Config, deps: Deps, by_name: dict,
                      intent: intents.Intent) -> None:
    issue = intent.issue
    task = load(cfg.state_dir, issue)
    if intent.action == "reply":
        if task is None or task.park not in (PARK_HUMAN, PARK_REVIEW):
            print(f"[warn] reply intent for #{issue}: task not parked for "
                  f"input — skipped", file=sys.stderr)
            return
        _wake(cfg, task, intent.payload.get("text", ""))
    elif intent.action == "park":
        if (task is None or task.stage not in IN_FLIGHT_STAGES or task.park
                or not deps.sessions.is_alive(issue)):
            print(f"[warn] park intent for #{issue}: no live unparked task "
                  f"— skipped", file=sys.stderr)
            return
        target = by_name.get(task.target)
        if target is None:
            print(f"[warn] park intent for #{issue}: target "
                  f"{task.target!r} left the config — skipped", file=sys.stderr)
            return
        _park_for_input(cfg, deps, target, task, note="parked by operator")
    elif intent.action == "kill":
        deps.sessions.end(issue)
        if task is not None:
            target = by_name.get(task.target)
            if target is not None:
                try:
                    deps.github.release(target, issue, "abandoned by operator")
                except Exception as exc:
                    print(f"[warn] release failed while killing #{issue}: "
                          f"{exc}", file=sys.stderr)
            # Write a FAILED tombstone instead of deleting the state file.
            # github.release() flips the board status back to Ready+auto, so
            # the issue becomes a live candidate again. Without a tombstone,
            # _claim_new's guard (known = {t.issue for t in tasks}) would not
            # contain it and would re-claim the just-killed issue the same pass.
            save(cfg.state_dir, replace(task, stage=Stage.FAILED,
                                        updated_at=_now()))
        clear_waiting(cfg.state_dir, issue)
        eventlog.append_event(cfg.state_dir, "failed",
                              target=task.target if task else "", issue=issue,
                              stage=task.stage.value if task else "",
                              actor=intent.actor, detail="killed by operator")
    elif intent.action == "retry":
        # Mirror check_quarantine's clear path (failures.py:160): drop the
        # fingerprint marker too, or the dedupe silently swallows the next
        # report of the same failure.
        for target in cfg.targets:
            qp = failures.quarantine_path(cfg.state_dir, target.name, issue)
            if not qp.exists():
                continue
            try:
                fp = json.loads(qp.read_text()).get("fingerprint")
            except (json.JSONDecodeError, OSError):
                fp = None
            if fp:
                failures.fingerprint_path(cfg.state_dir, fp).unlink(
                    missing_ok=True)
            qp.unlink(missing_ok=True)
    elif intent.action == "resume":
        if task is None or not task.park:
            print(f"[warn] resume intent for #{issue}: task not parked — "
                  f"skipped", file=sys.stderr)
            return
        _wake(cfg, task,
              intent.payload.get("text")
              or "The operator resumed this task. Continue.", hold=False)
    else:
        print(f"[warn] unknown intent action {intent.action!r} for #{issue}",
              file=sys.stderr)


def _apply_intents(cfg: Config, deps: Deps) -> None:
    """Drain operator intents (web console writes) at the top of the pass.
    Applied-then-deleted = at-most-once; a failed intent is deleted too,
    noted to stderr, and never aborts the pass or the remaining intents."""
    by_name = {t.name: t for t in cfg.targets}
    for intent in intents.list_intents(cfg.state_dir):
        try:
            _apply_one_intent(cfg, deps, by_name, intent)
            eventlog.append_event(cfg.state_dir, "intent-applied",
                                  issue=intent.issue, actor=intent.actor,
                                  detail=intent.action)
        except Exception as exc:
            print(f"[warn] intent {intent.path.name} failed: {exc}",
                  file=sys.stderr)
        finally:
            intents.delete_intent(intent)


SNAPSHOT_TTL_SECONDS = 7 * 24 * 3600


def _prune_snapshots(cfg: Config) -> None:
    """A snapshot outlives its task by at most a week. In-flight tasks keep
    theirs regardless of age — the console may need it while they are
    parked. Every pass checks; ~200 KB per file makes eagerness cheap."""
    root = Path(cfg.state_dir) / "snapshots"
    if not root.exists():
        return
    now = time.time()
    for p in root.glob("task-*.txt"):
        try:
            issue = int(p.stem.removeprefix("task-"))
        except ValueError:
            continue
        try:
            t = load(cfg.state_dir, issue)
        except Exception:
            continue  # unreadable/corrupt state file: skip, never abort the sweep
        if t is not None and t.stage in IN_FLIGHT_STAGES:
            continue
        try:
            if now - p.stat().st_mtime > SNAPSHOT_TTL_SECONDS:
                p.unlink()
        except OSError:
            continue


def run_pass(cfg: Config, deps: Deps, dry_run: bool = False) -> None:
    if not dry_run:
        _apply_intents(cfg, deps)
        _prune_snapshots(cfg)
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
            _drive_task(cfg, deps, target, task, budget_ok, dry_run)
        _wake_ci(cfg, deps, target)
        _poll_prs(cfg, deps, target)
        _resume_woken(cfg, deps, target, budget_ok)
        if budget_ok:
            _claim_new(cfg, deps, target, dry_run)
    _flush_done(cfg)


def guarded_pass(cfg: Config, deps: Deps, config_path: str,
                 dry_run: bool = False) -> None:
    try:
        run_pass(cfg, deps, dry_run=dry_run)
    except Exception:
        rep = failures.FailureReport(
            klass="pass-crash", target="", issue=0, title="(dispatcher)",
            error=traceback.format_exc(), log_tail="",
            repro=f"agent-ops-dispatcher --config {config_path}",
            worktree="")
        failures.report_failure(cfg, deps, rep, dry_run=dry_run)
        raise  # systemd must still see the unit fail


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
                sessions=Sessions(dry_run=args.dry_run, memory=cfg.session_memory, cpus=cfg.session_cpus, state_dir=cfg.state_dir),
                notifier=Notifier(dry_run=args.dry_run,
                                  console_url=cfg.console_url))
    if args.digest:
        send_digest(cfg, deps)
    else:
        with pass_lock(cfg.state_dir):
            guarded_pass(cfg, deps, args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
