"""One dispatcher pass: candidates → capacity → budget → claim → drive.

Stateless per pass — the view is rebuilt every time from the board (via
candidates), state files, stage.json signals, and session liveness. Run by a
systemd timer; one invocation = one pass."""
from __future__ import annotations

import argparse
import json
import logging
import re
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
from pathlib import Path
from typing import Callable

from dispatcher.convergence import pass_lock
from dispatcher.config import Config, Target, load_config, policy_for, referenced_providers
from dispatcher.usage import ProviderUsage, Verdict, admits, verdict_note
from dispatcher.usage_providers import ADAPTERS, fetch_all
from dispatcher import (claims, eventlog, execution_overrides, failures, intents, loops,
                        messages, pr_poll, queue_ops, relogin, tmux_migration,
                        triage)
from dispatcher.github import Candidate, GitHubClient

log = logging.getLogger(__name__)
from dispatcher import spec_publish, task_artifacts
from dispatcher.artifacts import TICKETS_DIR, ticket_files
from dispatcher.loops import Decision, Outcome, ResetCause
from dispatcher.machine import (ApplyDecision, ArmSpecApproval, HandleCrash, NoOp, Notify,
                                ParkForCI, ParkForInput, ParkForReview, PublishSpec,
                                RetryStage, SetTaskStage, StartTicket,
                                SpawnStage, next_actions)
from dispatcher.models import (Admitted, Entry, ModelPolicy, candidates,
                               override_refusal, parse_entry, pick_provider,
                               policy_stage, resolve, second_model, stage_pick,
                               track_from_labels, tracks_text)
from dispatcher.prompts import render_stage_prompt
from dispatcher.runtimes import runtime_for
from dispatcher.sessions import Sessions
from dispatcher.state import (TERMINAL_STAGES, IN_FLIGHT_STAGES, NO_SLOT, PARK_CI, PARK_HUMAN,
                              PARK_LOGIN, PARK_REVIEW, PARK_WAKE, WAKE_BLOCKED_PREFIX,
                              RESPAWNABLE_STAGES, AnswersRequest, SpecApprovalRequest,
                              Stage, TaskState, active, allocate_slot,
                              clear_waiting, delete, has_waiting,
                              holds_slot, load, load_all, max_slots,
                              next_stage, read_stage_signal, resumable_crash,
                              save, task_key)
from dispatcher.workspace import create_workspace, remove_workspace
import telegram.inbound as inbound
from telegram.inbound import Command, Plain, Reply
from telegram.notify import Notifier
from telegram.templates import task_ref

Admit = Callable[[str], Verdict]


@dataclass
class Deps:
    github: object
    sessions: object
    notifier: object


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cursor_now() -> str:
    """Now, truncated to whole seconds and stepped back one, for
    feedback_cursor only (same ISO shape as _now() otherwise).

    GitHub reports comment/review timestamps at second granularity: a
    comment posted at 10:00:00.9 comes back as "10:00:00Z". pr_poll's
    trigger is a strict `dt > cursor`, so a cursor of 10:00:00.5 — or of
    10:00:00, truncation alone changes nothing here — hides that comment
    from every future round even though it was posted AFTER the session
    that was supposed to see it started. Whole seconds minus one make the
    whole second the spawn happened in still readable, which is the side
    the spec picks: a redundant round beats a lost comment."""
    return (datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(seconds=1)).isoformat()


def _policy(cfg: Config, target: Target | None) -> ModelPolicy:
    """target is None for a task whose target has left the config — it still
    resolves, against the global policy."""
    return policy_for(cfg, target) if target else cfg.models


@dataclass(frozen=True)
class Launch:
    """The stage a spawn site is about to start and the entry it runs on,
    chosen once: the usage gate asks about exactly the model the spawner
    then launches, and the spawner records the entry as the stage's pick.
    `second` is the gated second model (models.second_model), decided with
    the launch and never stored, so every spawn and resume re-asks."""
    stage: Stage
    entry: Entry
    second: Entry | None = None

    @property
    def model(self) -> str:
        return self.entry.model_id


def _launch_for(cfg: Config, target: Target | None, task: TaskState,
                stage: Stage, admitted: Admitted) -> Launch | None:
    """The stage's recorded pick, else the first admitted entry of the task's
    track. None: the track is not configured, or nothing is admitted — wait.
    Review avoids the provider that ran implement."""
    policy = _policy(cfg, target)
    pick = stage_pick(task.picks, stage.value)
    if pick:
        return Launch(stage, parse_entry(pick, "pick"))
    if task.track not in policy.tracks:
        return None
    avoid = (pick_provider(task.picks, "implement")
             if policy_stage(stage.value) == "review" else "")
    entry = resolve(policy, task.track, stage.value, admitted, avoid)
    return Launch(stage, entry) if entry else None


def _admitted(admit: Admit, bypass: bool) -> Admitted:
    return (lambda m: True) if bypass else (lambda m: admit(m).admitted)


def _with_second(cfg: Config, target: Target | None, launch: Launch,
                 admit: Admit) -> Launch:
    """Grant the second model on the gate itself: a bypass never covers it."""
    second = second_model(_policy(cfg, target), launch.stage.value,
                          launch.entry, _admitted(admit, False))
    return replace(launch, second=second)


def _choose_launch(cfg: Config, target: Target | None, task: TaskState,
                   stage: Stage, admit: Admit) -> tuple[Launch | None, bool]:
    """What a spawn site launches and whether usage is bypassed. A one-shot
    operator override names the entry outright; otherwise the sticky pick or
    the track decides, and a denied launch is (None, bypass): nothing
    mutated, the signal persists, retried once headroom returns."""
    override = execution_overrides.load(cfg.state_dir, task.target, task.issue)
    bypass = bool(override and override.bypass_usage)
    if override is not None and override.model:
        launch = Launch(stage, parse_entry(override.model, "override"))
        if bypass or admit(launch.model).admitted:
            return _with_second(cfg, target, launch, admit), bypass
        return None, bypass
    launch = _launch_for(cfg, target, task, stage, _admitted(admit, bypass))
    if launch is None or (not bypass and not admit(launch.model).admitted):
        return None, bypass
    return _with_second(cfg, target, launch, admit), bypass


def _candidate_launch(cfg: Config, target: Target, cand: Candidate,
                      admit: Admit) -> tuple[Launch | None, bool]:
    """An unclaimed candidate has no picks: its spec entry comes from the
    track its labels name (else the untracked track)."""
    policy = policy_for(cfg, target)
    probe = TaskState(issue=cand.number, target=target.name, stage=Stage.QUEUED,
                      slot=NO_SLOT, worktree="", branch="", title=cand.title,
                      updated_at="", track=track_from_labels(cand.labels, policy))
    return _choose_launch(cfg, target, probe, Stage.SPEC, admit)


def _consume_execution_choice(cfg: Config, target: str, issue: int) -> None:
    execution_overrides.delete(cfg.state_dir, target, issue)


def _display_entry(cfg: Config, target: Target | None, task: TaskState) -> str:
    """The entry a task runs or would run next, for status lines: its pick for
    the current stage, else the first candidate, else ''."""
    policy = _policy(cfg, target)
    stage = next_stage(task)
    pick = stage_pick(task.picks, stage)
    if pick:
        return pick
    if task.track in policy.tracks:
        cands = candidates(policy, task.track, stage)
        if cands:
            return str(cands[0])
    return ""


def _log_model(worktree: str, stage: Stage, model: str) -> None:
    """Durable per-worktree breadcrumb. stage.json is co-owned — sessions
    overwrite it when they signal — so the log is the record that survives."""
    p = Path(worktree) / ".agent" / "models.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(f"{_now()} {stage.value} {model}\n")


def _url(target: Target, issue: int) -> str:
    return f"https://github.com/{target.repo}/issues/{issue}"


def _queue_message(cfg: Config, target: str, issue: int, text: str,
                   actor: str) -> None:
    """Every message the agent should eventually read goes here — operator
    replies AND dispatcher-authored ones (CI conclusions, "the operator
    resumed this"). Appending instead of overwriting one field is the whole
    point: a second message can no longer clobber the first.

    Blank text is not a message and never enters the queue: /attach wakes a
    task with text="" (it is a wake, not something to say), and a queued
    zero-length message would show a phantom ✉ badge on the card, an empty
    row in the console thread, and an empty entry under "## Operator
    messages" in the next resume prompt."""
    if not text.strip():
        return
    messages.append(cfg.state_dir, target, issue, text, actor)


def _message_block(msgs: list[messages.Message]) -> str:
    """The block appended to a spawn/resume prompt, oldest first. Actor and
    timestamp are included so the agent can tell an operator instruction from
    a dispatcher-authored CI note."""
    if not msgs:
        return ""
    lines = ["## Operator messages", ""]
    lines += [f"- [{m.created_at}] {m.actor}: {m.text}" for m in msgs]
    return "\n".join(lines)


def _drain(cfg: Config, target: str, issue: int) -> tuple[str, list[str]]:
    """(block, ids) for everything queued on this issue. The caller stamps
    the ids only AFTER the session actually took the prompt, so a spawn that
    raises leaves the mail queued for the next attempt. Ids are captured
    here, not re-derived later, so a message that arrives during the spawn is
    not stamped as delivered without ever being shown."""
    msgs = messages.undelivered(cfg.state_dir, target, issue)
    return _message_block(msgs), [m.id for m in msgs]


def _wake(cfg: Config, task: TaskState, text: str, hold: bool = False,
          actor: str = "dispatcher", *, model_override: str = "",
          bypass_usage: bool = False) -> None:
    _queue_message(cfg, task.target, task.issue, text, actor)
    task = loops.reset(task, ResetCause.OPERATOR_WAKE)
    save(cfg.state_dir, replace(task, park=PARK_WAKE, hold_for_attach=hold,
                                resume_model_override=model_override,
                                resume_bypass_usage=bypass_usage,
                                updated_at=_now()))


def _inject_login_code(cfg: Config, deps: Deps, task: TaskState,
                       code: str) -> None:
    """Reply to a login park = the OAuth authorization code. Raw keystrokes
    into the still-alive pane — NOT the wake/resume path, which would spawn
    a resumed session over the live login prompt. Park cleared; stage
    signals, the Stop hook, and the stall timer take over. A wrong code
    leaves the screen static and the stall detector simply re-fires.

    The reply may arrive hours later, so the prompt is re-verified first: the
    session's pane is a HOST shell (is_alive means the tab's shell is busy,
    not that the CLI is at a prompt), and once the CLI has exited the pane is
    back at the host shell, where send_text would execute the operator's text
    as a shell command outside the sandbox.

    Un-parking then restores the same invariant _resume_woken and _retry_plan
    enforce — no waiting marker, a `working` signal — or the very next pass
    re-reads blocked/awaiting-ci, re-parks, and ENDS the session that was
    just re-authenticated."""
    if relogin.classify_login(
            deps.sessions.capture_tail(task.target, task.issue)) is None:
        deps.notifier.send("status", lines=[
            f"{_ref(cfg, task)} is no longer at a login prompt — code NOT typed "
            f"(the pane would have run it as a shell command). Still parked; "
            f"attach to the session to sort it out."])
        return
    deps.sessions.send_text(task.target, task.issue, code.strip())
    clear_waiting(cfg.state_dir, task.target, task.issue)
    signal = read_stage_signal(task.worktree)
    if signal is not None and signal.status != "working":
        by_name = {t.name: t for t in cfg.targets}
        model = _display_entry(cfg, by_name.get(task.target), task)
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
        model = _display_entry(cfg, by_name.get(t.target), t)
        slot = "(no slot)" if t.slot == NO_SLOT else f"(slot {t.slot})"
        lines.append(f"{_ref(cfg, t)} {t.title} — {t.stage.value} [{model}]"
                     + (f" [{t.park}]" if t.park else "") + f" {slot}")
    lines = lines or ["(nothing in flight)"]
    # The sweep holds a real capacity unit that active() cannot see (its work
    # is a herdr tab, not a TaskState) — _run_pass reduces effective
    # capacity for it, so status must count it too or it reports 1/2 on a full
    # box for as long as the sweep runs.
    held = 1 if triage.running() else 0
    if held:
        lines.append("triage sweep running (holds 1 slot)")
    lines.append(f"capacity {len(active(tasks)) + held}/{cfg.capacity}")
    return lines


def _ref(cfg: Config, task: TaskState) -> str:
    return task_ref(task.target, task.issue, len(cfg.targets) > 1)


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


# A failing gh or rank call answers in chat instead of crashing the pass.
_COMMAND_ERRORS = (subprocess.CalledProcessError, OSError, LookupError,
                   ValueError)


def _reply_parked(tasks: list[TaskState]) -> list[TaskState]:
    # Both parks answer the same way — a reply is text for the session. They
    # stay distinct kinds so /status and the board can say "needs input
    # mid-stage" vs "spec ready, review at leisure".
    return [t for t in tasks if t.park in (PARK_HUMAN, PARK_REVIEW)]


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
    """A bare number names one task only while no other target has the same
    number parked; when two do, wake nothing and list them."""
    match = [t for t in tasks if t.issue == issue and t.park]
    if len(match) == 1:
        _wake(cfg, match[0], "", hold=True)
    elif match:
        deps.notifier.send("status", lines=(
            [f"#{issue} is ambiguous; parked tasks with that number:"]
            + [_ref(cfg, t) for t in match]))
    else:
        deps.notifier.send("status", lines=[f"#{issue} is not parked"])


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
    login = [t for t in tasks
             if t.park == PARK_LOGIN and t.park_msg_id == ev.reply_to_msg_id]
    match = [t for t in _reply_parked(tasks)
             if t.park_msg_id == ev.reply_to_msg_id]
    if login:
        _inject_login_code(cfg, deps, login[0], ev.text)
    elif match:
        _wake(cfg, match[0], ev.text)
    else:
        deps.notifier.send("status",
                           lines=["(reply didn't match any parked task)"])


def _handle_plain(cfg: Config, deps: Deps, reply_parked: list[TaskState],
                  text: str) -> None:
    if len(reply_parked) == 1:
        _wake(cfg, reply_parked[0], text)
    else:
        deps.notifier.send("status", lines=(
            ["Which task? Reply directly to its parked message:"]
            + [f"{_ref(cfg, t)} {t.title}" for t in reply_parked]))


def _notify(deps: Deps, target: Target, task: TaskState, template: str,
            note: str = "") -> None:
    deps.notifier.send(template, issue=task.issue, title=task.title,
                       url=_url(target, task.issue), note=note,
                       target=target.name)


def _spawn_stage(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 launch: Launch, spec_path: str = "", ticket: int = 0) -> TaskState:
    stage, entry = launch.stage, launch.entry
    model = entry.model_id
    ticket_path = ""
    if ticket:
        files = ticket_files(Path(task.worktree) / TICKETS_DIR)
        if ticket > len(files):
            raise RuntimeError(f"ticket {ticket} of {task.ticket_count} missing "
                               f"under {task.worktree}/{TICKETS_DIR}")
        ticket_path = str(files[ticket - 1].relative_to(task.worktree))
    ctx = dict(
        issue_number=task.issue, issue_title=task.title,
        issue_url=_url(target, task.issue), repo=target.repo,
        branch=task.branch, slot=task.slot,
        backend_port=8100 + task.slot, frontend_port=5200 + task.slot,
        verify_cmd=target.verify_cmd.format(slot=task.slot),
        gate_cmd=target.gate_cmd.format(slot=task.slot),
        spec_path=spec_path or task.spec_path,
        tickets_dir=TICKETS_DIR, ticket_number=ticket,
        ticket_count=task.ticket_count, ticket_path=ticket_path,
        pr_number=task.pr_number,
        reason=task.attention or "feedback",
        labels=", ".join(task.labels),
        tracks=tracks_text(_policy(cfg, target)),
    )
    prompt = render_stage_prompt(stage, ctx)
    block, drained = _drain(cfg, task.target, task.issue)
    if block:
        prompt = f"{prompt}\n\n{block}\n"
    agent_dir = Path(task.worktree) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stage.json").write_text(json.dumps(
        {"stage": stage.value, "status": "working", "model": model,
         "effort": entry.effort}))
    _log_model(task.worktree, stage, str(entry))
    deps.sessions.spawn_stage(task.target, task.issue, task.worktree, prompt,
                              stage.value, model, entry.effort,
                              second=launch.second)
    messages.mark_delivered(cfg.state_dir, task.target, task.issue, drained)
    # A fresh stage is a fresh budget for the loops it runs; ci_rounds belongs
    # to the PR, not the stage, and is reset by _poll_prs/_resume_one.
    task = loops.reset(task, ResetCause.STAGE_STARTED)
    task = replace(task, stage=stage, spec_path=spec_path or task.spec_path,
                   operator_request=None, updated_at=_now(),
                   picks={**task.picks, policy_stage(stage.value): str(entry)})
    save(cfg.state_dir, task)
    eventlog.append_event(cfg.state_dir, "stage-started", target=target.name,
                          issue=task.issue, stage=stage.value, model=str(entry),
                          detail=f"ticket {ticket}/{task.ticket_count} {ticket_path}" if ticket else "")
    return task


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


def _auth_dark_edge(cfg: Config, deps: Deps, usages: dict[str, ProviderUsage]) -> None:
    """One alert per unknowable-usage incident (auth likely dead). Dark means
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
    try:
        st = json.loads(marker.read_text())
        if st.get("alerted"):
            return
        age_min = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(st["since"])).total_seconds() / 60
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
        marker.write_text(json.dumps({"since": _now(), "alerted": False}))
        return
    if age_min >= AUTH_DARK_GRACE_MINUTES:
        deps.notifier.send("auth_dark", minutes=int(age_min),
                           host=socket.gethostname())
        marker.write_text(json.dumps({"since": st["since"], "alerted": True}))


def _park_for_input(cfg: Config, deps: Deps, target: Target, task: TaskState,
                    note: str, artifact: str = "",
                    is_answers: bool = False) -> None:
    tail = deps.sessions.capture_tail(task.target, task.issue)
    login = relogin.classify_login(tail)
    if login is not None and _park_for_login(cfg, deps, target, task, note,
                                             tail, login):
        return
    resolved = ""
    if artifact:
        p = Path(artifact)
        resolved = str(p if p.is_absolute() else Path(task.worktree) / p)
    answers_request: AnswersRequest | None = None  # cleared unless valid answers path is resolved below
    if resolved:
        wt_abs = Path(task.worktree).resolve()
        try:
            wt_rel = str(Path(resolved).resolve().relative_to(wt_abs))
            answers_request = AnswersRequest(path=wt_rel)
        except ValueError:
            # Path escapes the worktree — treat as unusable reference.
            resolved = ""
    if is_answers and answers_request is None:
        note = (note + "\n\n[malformed awaiting-answers: no usable artifact path]").strip()
    msg_id = deps.notifier.send(
        "parked_question", issue=task.issue, title=task.title,
        url=_url(target, task.issue), target=target.name,
        note=(note + ("\n\n" + tail if tail else "")).strip() or "(no detail)")
    deps.sessions.end(task.target, task.issue)
    clear_waiting(cfg.state_dir, task.target, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_HUMAN, park_msg_id=msg_id,
                                park_note=note, slot=NO_SLOT,
                                operator_request=answers_request,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, stage=task.stage.value, detail=note)


def _park_exhausted(cfg: Config, deps: Deps, target: Target, task: TaskState,
                    note: str) -> None:
    """A bounded loop hit its cap with no live session to end (a CI park, or
    a pr-open task): park for the operator. Their reply wakes it with a
    fresh budget (_wake / the reply intent zero the counters)."""
    msg_id = deps.notifier.send(
        "parked_question", issue=task.issue, title=task.title,
        url=_url(target, task.issue), target=target.name, note=note)
    save(cfg.state_dir, replace(task, park=PARK_HUMAN, park_msg_id=msg_id,
                                park_note=note, slot=NO_SLOT, ci_run_id=0,
                                feedback_pending=False, operator_request=None,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail="loop exhausted: " + note)


def _apply_loop_decision(cfg: Config, deps: Deps, target: Target,
                         task: TaskState, decision: Decision,
                         park_exhausted) -> tuple[TaskState, bool]:
    """Shared executor for a loop policy Decision (from machine.ApplyDecision).
    Returns (updated_task, parked). Parked=True means the caller must return.

    park_exhausted(task, note) is injected by the caller: session path passes
    _park_for_input (live container — ends session, clears waiting, captures tail);
    CI/PR paths pass _park_exhausted (no live session)."""
    if decision.outcome is Outcome.UNCHANGED:
        return task, False
    task = replace(decision.apply_to(task), updated_at=_now())
    save(cfg.state_dir, task)
    eventlog.append_event(cfg.state_dir, "round", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail=decision.description)
    if decision.outcome is Outcome.EXHAUSTED:
        note = f"{decision.loop.value} loop exceeded its cap of {decision.cap} rounds"
        if decision.detail:
            note += f" ({decision.detail})"
        park_exhausted(task, note)
        return task, True
    if decision.outcome is Outcome.LAST_ROUND:
        _notify(deps, target, task, "last_round",
                f"{decision.loop.value} round {decision.round}/{decision.cap}")
    return task, False


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
        url=_url(target, task.issue), target=target.name,
        login_url=login.url or "(attach to the session to see the URL)",
        note=(note + ("\n\n" + tail if tail else "")).strip() or "(no detail)")
    if msg_id == 0:
        return False
    clear_waiting(cfg.state_dir, task.target, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_LOGIN, park_msg_id=msg_id,
                                park_note=note, operator_request=None,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail="needs re-login: " + note)
    return True


def _retry_plan(cfg: Config, deps: Deps, target: Target, task: TaskState,
                launch: Launch, reason: str) -> None:
    """Resume the plan session with the format-check failure, in place, rather
    than failing the task. The resume reads the transcript from the runtime's
    mounted home, so context survives ending the (zombie) session first —
    which we must do, or _launch would type the resume command INTO the
    stopped session's input box (same failure mode as spawning over a live
    session)."""
    entry = launch.entry
    agent_dir = Path(task.worktree) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Rewrite the signal to working BEFORE resuming, or the next pass re-reads
    # `done`, re-checks the still-unfixed plan, and burns the retry immediately.
    (agent_dir / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "working", "model": entry.model_id,
         "effort": entry.effort}))
    _log_model(task.worktree, Stage.PLAN, str(entry))
    clear_waiting(cfg.state_dir, task.target, task.issue)
    deps.sessions.end(task.target, task.issue)
    block, drained = _drain(cfg, task.target, task.issue)
    retry_text = (
        f"Your ticket set under .agent/tickets/ failed the pipeline's mechanical "
        f"check: {reason}. Fix it in place — files named NN-slug.md numbered "
        f"contiguously from 01 with no gaps or duplicates, each with a "
        f"'What to build' section, a 'Blocked by' line and at least one "
        f"unchecked '- [ ]' criterion — then re-write .agent/stage.json with "
        f'status "done". Do not re-plan from scratch; only fix the set.')
    if block:
        retry_text = f"{retry_text}\n\n{block}"
    deps.sessions.resume(task.target, task.issue, task.worktree, retry_text,
                         entry.model_id, entry.effort)
    messages.mark_delivered(cfg.state_dir, task.target, task.issue, drained)
    save(cfg.state_dir, replace(task, plan_retries=task.plan_retries + 1,
                                updated_at=_now()))
    _notify(deps, target, task, "plan_retry", reason)


def _retry_spec(cfg: Config, deps: Deps, target: Target, task: TaskState,
                launch: Launch, reason: str) -> None:
    """Resume the spec session with the track list, in place. Same shape as
    _retry_plan: rewrite the signal to working first, end the zombie, then
    --continue with the correction."""
    entry = launch.entry
    agent_dir = Path(task.worktree) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "working", "model": entry.model_id,
         "effort": entry.effort}))
    _log_model(task.worktree, Stage.SPEC, str(entry))
    clear_waiting(cfg.state_dir, task.target, task.issue)
    deps.sessions.end(task.target, task.issue)
    block, drained = _drain(cfg, task.target, task.issue)
    text = (f"Your .agent/stage.json was rejected: {reason}. Re-write the same "
            f"signal with a \"track\" field naming one of those tracks (the "
            f"list with each track's meaning is in your stage prompt).")
    if block:
        text = f"{text}\n\n{block}"
    deps.sessions.resume(task.target, task.issue, task.worktree, text,
                         entry.model_id, entry.effort)
    messages.mark_delivered(cfg.state_dir, task.target, task.issue, drained)
    save(cfg.state_dir, replace(task, spec_retries=task.spec_retries + 1,
                                updated_at=_now()))


def _park_for_ci(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 run_id: int) -> None:
    deps.sessions.end(task.target, task.issue)
    clear_waiting(cfg.state_dir, task.target, task.issue)
    save(cfg.state_dir, replace(task, park=PARK_CI, ci_run_id=run_id,
                                slot=NO_SLOT, operator_request=None,
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
    if cfg.spec_review_grace_minutes is None:
        return False
    try:
        since = datetime.fromisoformat(task.updated_at)
    except (TypeError, ValueError):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - since).total_seconds()
    return age >= cfg.spec_review_grace_minutes * 60


def _redact_git_error(msg: str) -> str:
    """First line of a git error message, with embedded credentials removed."""
    first = msg.split("\n")[0]
    return re.sub(r"://[^@\s]*@", "://<redacted>@", first)


def _spec_note(pub: "spec_publish.PublishResult") -> str:
    """Format a PublishResult into the one-line string added to Telegram notes."""
    if pub.error:
        return f"⚠️ spec is local only: {_redact_git_error(pub.error)}"
    return f"spec: {pub.url}"


def _park_for_review(cfg: Config, deps: Deps, target: Target,
                     task: TaskState, dry_run: bool = False) -> None:
    """Park a finished spec for a human to read whenever they wake up. The
    only park that also releases the SLOT: the spec stage never used the
    slot's ports and worktrees are per-issue, so resume can take any free
    slot — and freeing it is the whole point, since a held slot would cap the
    overnight run at max_slots(capacity) specs."""
    tail = deps.sessions.capture_tail(task.target, task.issue)
    note = tail.strip() or "(no detail)"
    if task.spec_path:
        pub = spec_publish.ensure_published(
            worktree=task.worktree, branch=task.branch,
            repo=target.repo, issue=task.issue,
            artifact=task.spec_path, dry_run=dry_run)
        note += f"\n{_spec_note(pub)}"
    # No msg_id == 0 guard here (unlike _park_for_login): if the ping fails,
    # the task is not stranded — the session is ended and the operator can still
    # reach it via /attach, the console `resume` intent, or plain-text wakes.
    # This matches the same reasoning in _park_for_input.
    msg_id = deps.notifier.send(
        "spec_parked", issue=task.issue, title=task.title,
        url=_url(target, task.issue), note=note, target=target.name)
    deps.sessions.end(task.target, task.issue)
    clear_waiting(cfg.state_dir, task.target, task.issue)
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
        if conclusion != "success":
            dec = loops.evaluate(task, loops.FailedRun(f"run {task.ci_run_id} {conclusion}"),
                                 cfg.loop_caps)
            task, parked = _apply_loop_decision(
                cfg, deps, target, task, dec,
                park_exhausted=lambda t, note: _park_exhausted(cfg, deps, target, t, note))
            if parked:
                continue
        reply = (f"E2E run {task.ci_run_id} concluded: {conclusion} — "
                 f"fetch logs with: gh run view {task.ci_run_id} --log-failed")
        _queue_message(cfg, task.target, task.issue, reply, "dispatcher")
        save(cfg.state_dir, replace(task, park=PARK_WAKE, ci_run_id=0,
                                    updated_at=_now()))


def _poll_prs(cfg: Config, deps: Deps, target: Target,
              dry_run: bool = False) -> None:
    """Watch every pr-open task's PR independently of CI access and capacity.
    Reaction (address-review spawn) is gated
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
            payload = deps.github.pr_view(target, task.pr_number)
            login = deps.github.viewer_login()
            res = pr_poll.classify(payload, task.feedback_cursor, login,
                                   check_cursor=task.check_cursor,
                                   conflict_cursor=task.conflict_cursor)
            if res.kind in ("quiet", "conflict") and not task.park and not task.feedback_pending:
                statuses = deps.github.ci_statuses(
                    target, payload.get("headRefOid") or "",
                    payload.get("headRefName") or "")
                res = pr_poll.classify(payload, task.feedback_cursor, login,
                                       check_cursor=task.check_cursor,
                                       conflict_cursor=task.conflict_cursor,
                                       ci_statuses=statuses)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[warn] PR poll failed for #{task.issue}: {exc}",
                  file=sys.stderr)
            continue
        if res.kind == "merged":
            try:
                _finish_merged(cfg, deps, target, task, dry_run)
            except (subprocess.CalledProcessError, OSError) as exc:
                print(f"[warn] _finish_merged failed for #{task.issue}: {exc}",
                      file=sys.stderr)
                continue
        elif res.kind == "closed":
            # End the session too: the implement session is still alive at
            # pr-open, and nothing will ever drive this task again (FAILED
            # isn't in IN_FLIGHT_STAGES), so its session and container
            # would otherwise hold their memory/cpu reservation until the box
            # reboots. The worktree stays for autopsy.
            deps.sessions.end(task.target, task.issue)
            save(cfg.state_dir, replace(task, stage=Stage.FAILED,
                                        operator_request=None,
                                        updated_at=_now()))
            eventlog.append_event(cfg.state_dir, "pr-closed",
                                  target=target.name, issue=task.issue,
                                  stage=Stage.PR_OPEN.value,
                                  detail=f"PR #{task.pr_number} closed unmerged")
            _notify(deps, target, task, "pr_closed",
                    f"PR #{task.pr_number}. Worktree preserved for autopsy.")
        elif task.park:
            continue   # exhausted-loop park: only merge/close still matter
        elif res.kind == "feedback" and not task.feedback_pending:
            task = loops.reset(task, ResetCause.PR_CYCLE_STARTED)
            save(cfg.state_dir, replace(task, feedback_pending=True,
                                        attention="feedback",
                                        updated_at=_now()))
            eventlog.append_event(cfg.state_dir, "pr-feedback",
                                  target=target.name, issue=task.issue,
                                  stage=Stage.PR_OPEN.value,
                                  detail=f"newest {res.latest_ts}")
            _notify(deps, target, task, "pr_feedback",
                    f"PR #{task.pr_number}, newest feedback {res.latest_ts}")
        elif res.kind in ("check-failed", "conflict") and not task.feedback_pending:
            _pr_attention(cfg, deps, target, task, res)


def _pr_attention(cfg: Config, deps: Deps, target: Target, task: TaskState,
                  res: "pr_poll.PollResult") -> None:
    """A red check or a conflict on the open PR: one round of the ci loop,
    address-review queued with the reason, cursor advanced so the same
    condition never re-triggers."""
    cursor = ({"check-failed": dict(check_cursor=res.latest_ts),
               "conflict": dict(conflict_cursor=res.latest_ts)}[res.kind])
    task = replace(task, **cursor)          # advance cursor FIRST
    dec = loops.evaluate(task, loops.PRAttention(f"{res.kind} on PR #{task.pr_number}"),
                         cfg.loop_caps)
    task, parked = _apply_loop_decision(
        cfg, deps, target, task, dec,
        park_exhausted=lambda t, note: _park_exhausted(cfg, deps, target, t, note))
    if parked:
        return          # cursor already applied; _park_exhausted saved the cursor-advanced task
    # not exhausted: set feedback_pending + attention, emit pr-attention event, pr_attention notify
    save(cfg.state_dir, replace(task, feedback_pending=True, attention=res.kind,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "pr-attention", target=target.name,
                          issue=task.issue, stage=Stage.PR_OPEN.value,
                          detail=res.kind)
    _notify(deps, target, task, "pr_attention", f"{res.kind} on PR #{task.pr_number}")


def _finish_merged(cfg: Config, deps: Deps, target: Target,
                   task: TaskState, dry_run: bool = False) -> None:
    """Ordered so a mid-sequence gh failure retries next pass (a merged PR
    still reads merged): board first (the raising step), then teardown
    (best-effort by construction), then the state flip that stops polling."""
    if target.status_done_option_id:
        deps.github.set_status(target, task.issue, target.status_done_option_id)
    else:
        print(f"[warn] {target.name}: status_done_option_id unset — board "
              f"not updated for #{task.issue}", file=sys.stderr)
    deps.sessions.end(task.target, task.issue)
    if not dry_run:
        task_artifacts.collect(cfg.state_dir, task, target.repo)
        task_artifacts.pin_published(cfg.state_dir, task, target.repo)
    remove_workspace(target, task.worktree, task.branch, dry_run=dry_run)
    deps.github.delete_branch(target, task.branch)
    save(cfg.state_dir, replace(task, stage=Stage.DONE, park="",
                                feedback_pending=False, operator_request=None,
                                done_at=_now(), updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "merged", target=target.name,
                          issue=task.issue, stage=Stage.DONE.value,
                          detail=f"PR #{task.pr_number}")
    _notify(deps, target, task, "task_done",
            f"https://github.com/{target.repo}/pull/{task.pr_number}")


def _wake_blocked_path(cfg: Config, target: str, issue: int) -> Path:
    return (Path(cfg.state_dir)
            / f"{WAKE_BLOCKED_PREFIX}{task_key(target, issue)}")


def _mark_wake_blocked(cfg: Config, target: Target, task: TaskState,
                       reason: str) -> None:
    """Edge-triggered: the event fires the pass a wake FIRST goes hungry, not
    every pass thereafter — a resume that waits overnight must not write 144
    identical lines into events.jsonl. The marker is the edge."""
    p = _wake_blocked_path(cfg, task.target, task.issue)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    eventlog.append_event(cfg.state_dir, "wake-blocked", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail=reason)


def _clear_wake_blocked(cfg: Config, target: str, issue: int) -> None:
    _wake_blocked_path(cfg, target, issue).unlink(missing_ok=True)


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
            _clear_wake_blocked(cfg, task.target, task.issue)
            delete(cfg.state_dir, task.target, task.issue)
            eventlog.append_event(cfg.state_dir, "flushed",
                                  target=task.target, issue=task.issue,
                                  stage=Stage.DONE.value)


def _oldest_first(cfg: Config):
    """Sort key for box-wide queues: oldest `updated_at` first, ties in
    target-list order so a pass is deterministic."""
    rank = {t.name: i for i, t in enumerate(cfg.targets)}
    return lambda t: (t.updated_at, rank[t.target])


def _resume_woken(cfg: Config, deps: Deps, admit: Admit,
                  dry_run: bool = False) -> None:
    """Box-wide, oldest wake first across every target: capacity is shared,
    so target listing order must not decide whose approved work waits."""
    targets = {t.name: t for t in cfg.targets}
    woken = sorted(
        [t for t in load_all(cfg.state_dir)
         if t.target in targets and t.park == PARK_WAKE],
        key=_oldest_first(cfg),
    )
    for task in woken:
        target = targets[task.target]
        launch = _resume_launch(cfg, target, task, admit)
        if launch is None or (not task.resume_bypass_usage
                              and not admit(launch.model).admitted):
            continue  # this model's provider has no headroom; others may
        if _box_free(cfg, load_all(cfg.state_dir)) <= 0:
            _mark_wake_blocked(cfg, target, task, "capacity full")
            continue
        if task.slot == NO_SLOT:
            # Gate-parked tasks gave their slot back. Take any free one —
            # worktrees are per-issue and the spec stage never bound the
            # slot's ports, so the number need not be the original.
            slot = allocate_slot(load_all(cfg.state_dir),
                                 max_slots(cfg.capacity))
            if slot is None:
                _mark_wake_blocked(cfg, target, task, "no free slot")
                continue  # stays parked, retried next pass
            task = replace(task, slot=slot)
        try:
            _resume_one(cfg, deps, target, task, launch)
        except Exception:
            _fail_task_crash(cfg, deps, target, task, dry_run)


def _resume_launch(cfg: Config, target: Target, task: TaskState,
                   admit: Admit) -> Launch | None:
    """A parked pr-open task has no session to continue: it resumes as a
    fresh address-review round, on that stage's model."""
    stage = Stage.ADDRESS_REVIEW if task.stage is Stage.PR_OPEN else task.stage
    if task.resume_model_override:
        launch = Launch(stage, parse_entry(task.resume_model_override, "override"))
    else:
        launch = _launch_for(cfg, target, task, stage,
                             _admitted(admit, task.resume_bypass_usage))
    return _with_second(cfg, target, launch, admit) if launch else None


def _resume_one(cfg: Config, deps: Deps, target: Target,
                task: TaskState, launch: Launch) -> None:
    entry = launch.entry
    model = entry.model_id
    agent_dir = Path(task.worktree) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Rewrite stage.json BEFORE resuming, or the next pass re-reads
    # blocked/awaiting-ci and re-parks the freshly resumed session.
    (agent_dir / "stage.json").write_text(json.dumps(
        {"stage": task.stage.value, "status": "working", "model": model,
         "effort": entry.effort}))
    _log_model(task.worktree, task.stage, str(entry))
    # End first, unconditionally. Most parks already stopped the session,
    # but /attach on a PARK_LOGIN task reaches here with the pane still
    # LIVE, and _launch would then type the podman command INTO the
    # running session (the failure _retry_plan and SpawnStage guard).
    deps.sessions.end(task.target, task.issue)
    if task.crashed_stage:
        _respawn_crashed(cfg, deps, target, task, launch)
        return
    if task.stage is Stage.PR_OPEN:
        # No session to continue at pr-open: an operator wake on a parked
        # pr-open task is a fresh address-review round carrying their message.
        task = loops.reset(task, ResetCause.PR_CYCLE_STARTED)
        task = replace(task, park="", park_msg_id=0, park_note="",
                       hold_for_attach=False, feedback_pending=False,
                       attention="operator", feedback_cursor=_cursor_now(),
                       resume_model_override="", resume_bypass_usage=False,
                       operator_request=None)
        _clear_wake_blocked(cfg, task.target, task.issue)
        _spawn_stage(cfg, deps, target, task, launch)
        eventlog.append_event(cfg.state_dir, "resumed", target=target.name,
                              issue=task.issue, stage=Stage.ADDRESS_REVIEW.value,
                              model=str(entry))
        return
    block, drained = _drain(cfg, task.target, task.issue)
    if task.hold_for_attach:
        text = ("The operator is attaching to talk to you directly. "
                "Wait for their input.")
        if block:
            text = f"{text}\n\n{block}"
        deps.sessions.resume(task.target, task.issue, task.worktree, text,
                             model, entry.effort, second=launch.second)
        deps.notifier.send("resumed_for_attach", issue=task.issue,
                           title=task.title, url=_url(target, task.issue),
                           target=target.name, note="")
    else:
        deps.sessions.resume(task.target, task.issue, task.worktree,
                             block or "Continue.", model, entry.effort,
                             second=launch.second)
    messages.mark_delivered(cfg.state_dir, task.target, task.issue, drained)
    _clear_wake_blocked(cfg, task.target, task.issue)
    save(cfg.state_dir, replace(task, park="", hold_for_attach=False,
                                park_msg_id=0, park_note="",
                                resume_model_override="",
                                resume_bypass_usage=False,
                                operator_request=None,
                                picks={**task.picks,
                                      policy_stage(task.stage.value): str(entry)},
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "resumed", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          model=str(entry))


def _crash_failed(task: TaskState) -> TaskState:
    """FAILED by a crash, remembering the stage so Resume can respawn it."""
    crashed = task.stage.value if task.stage in RESPAWNABLE_STAGES else ""
    return replace(task, stage=Stage.FAILED, crashed_stage=crashed, park="",
                   hold_for_attach=False, operator_request=None,
                   updated_at=_now())


def _respawn_crashed(cfg: Config, deps: Deps, target: Target,
                     task: TaskState, launch: Launch) -> None:
    """Resume of a crashed task: the stage it died in starts afresh in the
    same worktree — the same ticket for implement. A fresh stage prompt, not
    a resume (`claude --continue` / `codex resume --last`): the newest
    transcript may belong to the previous stage or ticket, or the crashed
    launch may never have started one. The queued messages ride in the
    stage prompt."""
    deps.github.set_status(target, task.issue,
                           target.status_in_progress_option_id)
    task = replace(task, crashed_stage="", park="", park_msg_id=0,
                   park_note="", hold_for_attach=False,
                   resume_model_override="", resume_bypass_usage=False)
    _clear_wake_blocked(cfg, task.target, task.issue)
    ticket = task.ticket_cursor if task.stage is Stage.IMPLEMENT else 0
    task = _spawn_stage(cfg, deps, target, task, launch, ticket=ticket)
    eventlog.append_event(cfg.state_dir, "resumed", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          model=str(launch.entry), detail="after crash")


def _fail_task_crash(cfg: Config, deps: Deps, target: Target,
                     task: TaskState, dry_run: bool = False) -> None:
    """One task's turn blew up. Fail THAT task; let the pass carry on.

    Everything a task's turn touches is reachable through its worktree, and
    a worktree can simply be gone — swept, half-removed, or deleted by hand.
    containers.clone_root then raises reading <worktree>/.git, and before
    this the exception unwound all the way out of run_pass: guarded_pass
    filed a pass-crash and re-raised, so the unit failed and every task
    QUEUED BEHIND the broken one was never reached. One dead checkout stopped
    the whole box, every firing, until a human looked. A card in Failed is
    the far cheaper outcome, and the report carries the traceback."""
    error = traceback.format_exc()
    # Best-effort teardown: the point of this function is that nothing in it
    # may raise, or we are back to killing the pass.
    try:
        deps.github.release(target, task.issue, "task crashed mid-pass")
    except Exception as exc:
        print(f"[warn] release failed for #{task.issue}: {exc}", file=sys.stderr)
    try:
        log_tail = deps.sessions.capture_tail(task.target, task.issue, lines=30)
    except Exception:
        log_tail = ""
    save(cfg.state_dir, _crash_failed(task))
    eventlog.append_event(cfg.state_dir, "failed", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail="task crashed mid-pass")
    # klass is not "session-crash": nothing is wrong inside the container,
    # so this routes to infra_repo rather than the target repo.
    rep = failures.FailureReport(
        klass="task-crash", target=target.name, issue=task.issue,
        title=f"task #{task.issue} crashed mid-pass: {task.title}",
        error=error, log_tail=log_tail,
        repro=f"agent-ops-dispatcher  # with task-{task.issue} at "
              f"stage {task.stage.value}",
        worktree=task.worktree)
    failures.report_failure(cfg, deps, rep, dry_run=dry_run)


def _spawn_feedback(cfg: Config, deps: Deps, admit: Admit) -> None:
    """Spawn address-review for tasks whose PR got feedback, box-wide and
    oldest first — same gates as claiming new work (capacity, usage, slot);
    a denied spawn just stays pr-open+pending and retries next pass, badge
    showing."""
    targets = {t.name: t for t in cfg.targets}
    pending = sorted(
        [t for t in load_all(cfg.state_dir)
         if t.target in targets and t.stage is Stage.PR_OPEN
         and t.feedback_pending],
        key=_oldest_first(cfg))
    for task in pending:
        target = targets[task.target]
        launch, bypass_usage = _choose_launch(cfg, target, task,
                                              Stage.ADDRESS_REVIEW, admit)
        if launch is None:
            continue
        if _box_free(cfg, load_all(cfg.state_dir)) <= 0:
            _mark_wake_blocked(cfg, target, task, "capacity full")
            continue
        slot = allocate_slot(load_all(cfg.state_dir), max_slots(cfg.capacity))
        if slot is None:
            _mark_wake_blocked(cfg, target, task, "no free slot")
            continue
        _clear_wake_blocked(cfg, task.target, task.issue)
        # Cursor := spawn time: everything the session can read live is
        # now "seen"; anything arriving after this moment re-triggers a
        # round (conservative — a redundant round beats a lost comment).
        # _cursor_now(), not _now(): GitHub timestamps are second-granular
        # and pr_poll's trigger is a strict `>`, so the cursor has to be
        # quantised to seconds or a comment from the spawn second is lost.
        task = replace(task, slot=slot, feedback_pending=False,
                       feedback_cursor=_cursor_now())
        # The implement session is still alive at pr-open (the pr-open
        # transition never ends it). End first so _launch doesn't type
        # the podman command into the live session's input box.
        deps.sessions.end(task.target, task.issue)
        _spawn_stage(cfg, deps, target, task, launch)
        _consume_execution_choice(cfg, task.target, task.issue)


@dataclass
class _Turn:
    """One task's drive turn: what every action handler reads, plus the
    spec-publish line a later Notify in the same turn appends."""
    cfg: Config
    deps: Deps
    target: Target
    signal: object          # the stage signal read at the start of the turn
    dry_run: bool
    spec_line: str = ""


def _action_stage(act: object) -> Stage | None:
    """The stage an action launches a session for; None when it launches none."""
    if isinstance(act, StartTicket):
        return Stage.IMPLEMENT
    if isinstance(act, RetryStage):
        return act.stage  # _retry_plan/_retry_spec resume the session in place
    return act.stage if isinstance(act, SpawnStage) else None


# Handlers return the task to keep driving, or None to end the task's turn.

def _on_noop(turn: _Turn, task: TaskState, act: NoOp,
             launch: Launch | None) -> TaskState:
    return task


def _on_start_ticket(turn: _Turn, task: TaskState, act: StartTicket,
                     launch: Launch) -> TaskState:
    cfg, deps, target = turn.cfg, turn.deps, turn.target
    # Validate the requested ticket exists before any destructive side
    # effects (ending the previous session, advancing the cursor).
    # Missing file → raise now so _run_pass routes to _fail_task_crash
    # without having killed the old session or mutated state.
    if act.cursor > len(ticket_files(Path(task.worktree) / TICKETS_DIR)):
        raise RuntimeError(
            f"ticket {act.cursor} of {act.count} missing "
            f"under {task.worktree}/{TICKETS_DIR}")
    clear_waiting(cfg.state_dir, task.target, task.issue)
    deps.sessions.end(task.target, task.issue)
    task = replace(task, ticket_cursor=act.cursor, ticket_count=act.count)
    task = _spawn_stage(cfg, deps, target, task, launch, ticket=act.cursor)
    eventlog.append_event(cfg.state_dir, "ticket-started", target=target.name,
                          issue=task.issue, stage=Stage.IMPLEMENT.value,
                          detail=f"ticket {act.cursor}/{act.count}")
    return task


def _on_apply_decision(turn: _Turn, task: TaskState, act: ApplyDecision,
                       launch: Launch | None) -> TaskState | None:
    cfg, deps, target = turn.cfg, turn.deps, turn.target
    task, parked = _apply_loop_decision(
        cfg, deps, target, task, act.decision,
        park_exhausted=lambda t, note: _park_for_input(cfg, deps, target, t, note))
    return None if parked else task


def _on_park_for_input(turn: _Turn, task: TaskState, act: ParkForInput,
                       launch: Launch | None) -> None:
    _park_for_input(turn.cfg, turn.deps, turn.target, task, act.note,
                    artifact=act.artifact, is_answers=act.is_answers)


def _on_arm_spec_approval(turn: _Turn, task: TaskState, act: ArmSpecApproval,
                          launch: Launch | None) -> TaskState:
    # Re-establish spec-approval request cleared by a prior resume.
    # Do NOT touch updated_at — the resume already stamped it; leaving it
    # preserves the grace deadline. No stage transition.
    task = replace(task, operator_request=SpecApprovalRequest(),
                   spec_path=act.artifact or task.spec_path)
    save(turn.cfg.state_dir, task)
    return task


def _on_park_for_review(turn: _Turn, task: TaskState, act: ParkForReview,
                        launch: Launch | None) -> None:
    _park_for_review(turn.cfg, turn.deps, turn.target, task, dry_run=turn.dry_run)


def _on_park_for_ci(turn: _Turn, task: TaskState, act: ParkForCI,
                    launch: Launch | None) -> None:
    _park_for_ci(turn.cfg, turn.deps, turn.target, task, act.run_id)


def _on_retry_stage(turn: _Turn, task: TaskState, act: RetryStage,
                    launch: Launch) -> None:
    fn = _retry_spec if act.stage is Stage.SPEC else _retry_plan
    fn(turn.cfg, turn.deps, turn.target, task, launch, act.reason)


def _stage_extra(act: SetTaskStage, signal) -> dict:
    """The fields a stage transition sets besides the stage itself: the PR
    number a pr-open signal links, or the spec-approval request (and spec
    path) a finished spec arms."""
    if act.stage is Stage.AWAITING_SPEC_REVIEW:
        extra: dict = {"operator_request": SpecApprovalRequest()}
        if act.artifact:
            extra["spec_path"] = act.artifact
        if signal is not None:
            extra["track"] = signal.track
        return extra
    if act.stage is not Stage.PR_OPEN or signal is None:
        return {}
    m = re.search(r"/pull/(\d+)", signal.artifact or signal.note or "")
    return {"pr_number": int(m.group(1))} if m else {}


def _on_set_task_stage(turn: _Turn, task: TaskState, act: SetTaskStage,
                       launch: Launch | None) -> TaskState:
    cfg = turn.cfg
    clear_waiting(cfg.state_dir, task.target, task.issue)
    task = replace(task, stage=act.stage, updated_at=_now(),
                   **_stage_extra(act, turn.signal))
    save(cfg.state_dir, task)
    if act.stage is Stage.PR_OPEN:
        eventlog.append_event(cfg.state_dir, "pr-opened",
                              target=turn.target.name, issue=task.issue,
                              stage=act.stage.value)
    return task


def _on_publish_spec(turn: _Turn, task: TaskState, act: PublishSpec,
                     launch: Launch | None) -> TaskState:
    pub = spec_publish.ensure_published(
        worktree=task.worktree, branch=task.branch,
        repo=turn.target.repo, issue=task.issue,
        artifact=act.artifact, dry_run=turn.dry_run)
    turn.spec_line = _spec_note(pub)
    if not pub.error:
        try:
            turn.deps.github.comment(
                turn.target, task.issue, f"📝 Spec ready for review: {pub.url}")
        except Exception as exc:
            print(f"[warn] spec link comment failed for "
                  f"#{task.issue}: {exc}", file=sys.stderr)
    return task


def _on_notify(turn: _Turn, task: TaskState, act: Notify,
               launch: Launch | None) -> TaskState:
    note = act.note + (f"\n{turn.spec_line}" if turn.spec_line else "")
    _notify(turn.deps, turn.target, task, act.template, note)
    return task


def _on_spawn_stage(turn: _Turn, task: TaskState, act: SpawnStage,
                    launch: Launch) -> TaskState:
    clear_waiting(turn.cfg.state_dir, task.target, task.issue)
    # The previous stage's session is usually still alive here — an
    # interactive session cannot exit itself. _launch would type
    # the next stage's podman command INTO it (and the container
    # name would collide). End it first; no-op when already dead.
    turn.deps.sessions.end(task.target, task.issue)
    spec_path = turn.signal.artifact if act.stage is Stage.PLAN else ""
    if act.stage is Stage.PLAN:
        task = replace(task, track=turn.signal.track)
    return _spawn_stage(turn.cfg, turn.deps, turn.target, task, launch, spec_path)


def _on_handle_crash(turn: _Turn, task: TaskState, act: HandleCrash,
                     launch: Launch | None) -> TaskState:
    cfg, deps, target = turn.cfg, turn.deps, turn.target
    _notify(deps, target, task, "session_crashed")
    deps.github.release(target, task.issue, "session crashed mid-stage")
    save(cfg.state_dir, _crash_failed(task))
    eventlog.append_event(cfg.state_dir, "failed", target=target.name,
                          issue=task.issue, stage=task.stage.value,
                          detail="session crashed mid-stage")
    _report_session_crash(cfg, deps, target, task, turn.dry_run)
    # End last: _report_session_crash reads the pane first. end()
    # snapshots the crash output for the console and closes the
    # dead tab — the one session-ending transition that otherwise
    # left both behind.
    deps.sessions.end(task.target, task.issue)
    return task


_DRIVE: dict[type, Callable[..., TaskState | None]] = {
    NoOp: _on_noop,
    StartTicket: _on_start_ticket,
    ApplyDecision: _on_apply_decision,
    ParkForInput: _on_park_for_input,
    ArmSpecApproval: _on_arm_spec_approval,
    ParkForReview: _on_park_for_review,
    ParkForCI: _on_park_for_ci,
    RetryStage: _on_retry_stage,
    SetTaskStage: _on_set_task_stage,
    PublishSpec: _on_publish_spec,
    Notify: _on_notify,
    SpawnStage: _on_spawn_stage,
    HandleCrash: _on_handle_crash,
}


def _drive_task(cfg: Config, deps: Deps, target: Target, task: TaskState,
                admit: Admit, dry_run: bool = False) -> None:
    signal = read_stage_signal(task.worktree)
    policy = policy_for(cfg, target)
    if not task.track:
        # Claimed before tracks existed (#121): no triage label was read and
        # its spec signal names none. Run it as untracked work, the same as
        # a candidate without a track label, and record that for the console.
        task = replace(task, track=policy.untracked)
        if not dry_run:
            save(cfg.state_dir, task)
        if signal is not None and not signal.track:
            signal = replace(signal, track=task.track)
    # A spec-stage signal carries the track for every later stage (see
    # StageSignal.track). Adopt it before anything below reads task.track —
    # the "track configured" guard and the launch this same turn may spawn
    # (e.g. PLAN off a spec "done" signal) both need the fresh value, not
    # whatever was recorded when the task was last saved. Only a CONFIGURED
    # track is adopted here: an unknown/misspelled one must reach
    # next_actions' bounce-then-park ladder (_track_actions) instead of being
    # written onto the task and mis-parked as "no longer configured".
    if (signal is not None and signal.track and signal.track != task.track
            and signal.track in policy.tracks):
        task = replace(task, track=signal.track)
    alive = deps.sessions.is_alive(task.target, task.issue)
    waiting = has_waiting(cfg.state_dir, task.target, task.issue)
    if task.track not in policy.tracks:
        _park_for_input(cfg, deps, target, task,
                        f"track {task.track!r} is no longer configured (have "
                        f"{sorted(policy.tracks)}); restore it in targets.yaml or "
                        f"run the task with a model override")
        return
    # Query idle only when it can matter: detection enabled and the
    # session alive (the crash path owns dead sessions).
    idle = (deps.sessions.idle_seconds(task.target, task.issue)
            if alive and cfg.stall_after_seconds > 0 else None)
    turn = _Turn(cfg, deps, target, signal, dry_run)
    for act in next_actions(task, signal, alive, waiting=waiting,
                            idle_seconds=idle,
                            stall_after=cfg.stall_after_seconds,
                            grace_elapsed=_grace_elapsed(cfg, task),
                            caps=cfg.loop_caps,
                            tracks=frozenset(policy.tracks)):
        stage = _action_stage(act)
        launch, bypass_usage = ((None, False) if stage is None
                                else _choose_launch(cfg, target, task, stage, admit))
        if stage is not None and launch is None:
            return  # nothing mutated; the signal persists; retried once headroom returns
        choice_key = (task.target, task.issue)
        task = _DRIVE[type(act)](turn, task, act, launch)
        if launch is not None:
            _consume_execution_choice(cfg, *choice_key)
        if task is None:
            return


def _report_session_crash(cfg: Config, deps: Deps, target: Target,
                          task: TaskState, dry_run: bool) -> None:
    # The stage's pick names its runtime; a pre-picks task ran on Claude,
    # which is what a bare (here: empty) id resolves to.
    pick = stage_pick(task.picks, task.stage.value)
    runtime = runtime_for(parse_entry(pick, "pick").model_id if pick else "")
    rep = failures.FailureReport(
        klass="session-crash", target=target.name, issue=task.issue,
        title=f"session crashed during {task.stage.value}: {task.title}",
        error=(f"session task-{task.target}-{task.issue} died during stage "
               f"{task.stage.value}"),
        log_tail=deps.sessions.capture_tail(task.target, task.issue, lines=30),
        repro=f"cd {task.worktree} && {runtime.resume_cmd()}  # inside session image",
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
        repro=(f"podman run --rm -v {wt}:{wt} -w {wt} agent-ops-session "
               f"{target.setup_cmd}" if target.setup_cmd
               else "no setup_cmd; the worktree step failed (see error)"),
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


def _box_free(cfg: Config, tasks: list[TaskState]) -> int:
    """claims.box_free for this pass. `cfg` is the pass's effective config,
    whose capacity already has a running triage sweep's unit taken off."""
    return claims.box_free(cfg.capacity, tasks, triage_running=False)


def _reopened(stale: TaskState, pass_started: str) -> bool:
    """Reopened won't-do: rank drops CLOSED issues, so a candidate row for a
    canceled task proves the operator reopened the issue and moved the card
    back to Ready; its tombstone is swept for a fresh claim. The pass_started
    guard keeps a tombstone written by THIS pass's cancel intent (its board
    write lagging or failed) from resurrecting the task it just retired."""
    return (stale.stage is Stage.CANCELED
            and not (pass_started and stale.updated_at >= pass_started))


def _claimable(cfg: Config, deps: Deps, target: Target, tasks: list[TaskState],
               admit: Admit, pass_started: str):
    """Ranked candidates this pass may claim, each with the spec launch it
    would start. Skips issues that already have a task, quarantined issues,
    and candidates whose spec model has no headroom — a denied candidate
    leaves the next one eligible, since its model may differ."""
    by_issue = {t.issue: t for t in tasks}
    for cand in deps.github.candidates(target):
        stale = by_issue.get(cand.number)
        if stale is not None:
            if not _reopened(stale, pass_started):
                continue
            delete(cfg.state_dir, target.name, cand.number)
            del by_issue[cand.number]
            eventlog.append_event(cfg.state_dir, "reopened",
                                  target=target.name, issue=cand.number,
                                  detail="canceled task ranked again — "
                                         "reopened by operator")
        if failures.check_quarantine(cfg.state_dir, deps.github, target.name,
                                     cand.number):
            continue
        launch, _bypass = _candidate_launch(cfg, target, cand, admit)
        if launch is not None:
            yield cand, launch


def _round_counts(targets: list[Target],
                  all_tasks: list[TaskState]) -> dict[str, int]:
    """Each target's current active-task count, seeded at 0 so every target
    in the round has an entry even with nothing running yet."""
    counts = {t.name: 0 for t in targets}
    for t in active(all_tasks):
        if t.target in counts:
            counts[t.target] += 1
    return counts


def _commit_claim(cfg: Config, deps: Deps, target: Target, cand: Candidate,
                  slot: int, wt: str, launch: Launch) -> TaskState:
    """Board mutation is last and irreversible: state is saved and the
    `claimed` event appended first, so a crash before the claim leaves a
    recoverable partial claim rather than a silently lost slot. A failure
    from here on releases the board claim back to Ready."""
    task = TaskState(issue=cand.number, target=target.name,
                     stage=Stage.QUEUED, slot=slot, worktree=wt,
                     branch=f"agent/task-{cand.number}",
                     title=cand.title, updated_at=_now(),
                     effort=cand.effort, labels=cand.labels,
                     track=track_from_labels(cand.labels, policy_for(cfg, target)))
    save(cfg.state_dir, task)  # state exists BEFORE the irreversible claim, so a partial claim is recoverable
    eventlog.append_event(cfg.state_dir, "claimed", target=target.name,
                          issue=cand.number, stage=Stage.QUEUED.value)
    try:
        deps.github.claim(target, cand)  # irreversible board mutation — last
        task = _spawn_stage(cfg, deps, target, task, launch)
        _consume_execution_choice(cfg, target.name, cand.number)
    except Exception:
        deps.github.release(target, cand.number, "claim/spawn failed after provisioning")
        raise
    return task


def _claim_new(cfg: Config, deps: Deps, targets: list[Target],
               admit: Admit, dry_run: bool, pass_started: str = "") -> None:
    """Claim free units one at a time, each via claims.pick_target. A target leaves
    the round when its candidates run out or provisioning fails for it."""
    all_tasks = load_all(cfg.state_dir)
    free = _box_free(cfg, all_tasks)
    if free <= 0:
        return
    counts = _round_counts(targets, all_tasks)
    last = claims.last_claims(cfg.state_dir)
    caps = {t.name: t.max_active for t in targets}
    # Generator bodies run on first next(), so a target's rank_cmd runs only
    # when it gets a turn.
    gens = {t.name: _claimable(cfg, deps, t,
                               [x for x in all_tasks if x.target == t.name],
                               admit, pass_started)
            for t in targets}
    in_round = {t.name: t for t in targets}  # insertion order = list order
    while free > 0:
        name = claims.pick_target(list(in_round), counts, last, caps)
        if name is None:
            return
        target = in_round[name]
        picked = next(gens[target.name], None)
        if picked is None:
            del in_round[name]
            continue
        cand, launch = picked
        slot = allocate_slot(load_all(cfg.state_dir), max_slots(cfg.capacity))
        if slot is None:
            return
        try:
            wt = create_workspace(target, cand.number, dry_run=dry_run)
        except Exception:
            # Board never claimed (claim is last, still Ready) → no release
            # needed; report + quarantine, and the pass survives.
            _report_provisioning_failure(cfg, deps, target, cand, dry_run)
            # Cap at one provisioning failure per target per pass: a
            # systemic fault (git remote down, podman down, worktrees
            # volume full) would otherwise fail EVERY remaining Ready
            # candidate, filing an issue + ping + quarantine record per
            # candidate. The target leaves this pass's round; the next pass
            # retries its remaining candidates, and other targets keep claiming.
            del in_round[name]
            continue
        _commit_claim(cfg, deps, target, cand, slot, wt, launch)
        counts[target.name] += 1
        last[target.name] = _now()
        free -= 1  # checked before the next pull, so no candidate is vetted (or swept) that cannot be claimed


def _task_for_intent(cfg: Config, intent: intents.Intent) -> TaskState | None:
    """Resolve an intent to the task it targets. A target-carrying intent
    (every intent the console writes now) is an exact lookup. A legacy
    intent (target == "", written before this keying existed) falls back to
    matching by issue number alone — ambiguous when more than one target has
    a task with that number, in which case there is no safe task to act on
    and the intent is skipped rather than guessed at."""
    if intent.target:
        return load(cfg.state_dir, intent.target, intent.issue)
    hits = [t for t in load_all(cfg.state_dir) if t.issue == intent.issue]
    return hits[0] if len(hits) == 1 else None   # ambiguous legacy → skip


def _apply_reply_intent(cfg: Config, deps: Deps, task: TaskState | None,
                        intent: intents.Intent) -> None:
    """Deliver a reply, or inject it when a login prompt owns the pane."""
    if task is not None and task.park == PARK_LOGIN:
        _inject_login_code(cfg, deps, task, intent.payload.get("text", ""))
        return
    # A legacy intent (target "") names its project only through its task,
    # or through the sole configured target; otherwise it is not guessed at.
    target = (intent.target or (task.target if task is not None else "")
              or (cfg.targets[0].name if len(cfg.targets) == 1 else ""))
    if not target:
        print(f"[warn] reply for #{intent.issue} dropped: legacy intent "
              f"names no target", file=sys.stderr)
        return
    _queue_message(cfg, target, intent.issue, intent.payload.get("text", ""),
                   intent.actor or "operator")
    if task is not None and task.park in (PARK_HUMAN, PARK_REVIEW):
        task = loops.reset(task, ResetCause.OPERATOR_WAKE)
        save(cfg.state_dir, replace(task, park=PARK_WAKE, updated_at=_now()))


def _parkable_task(deps: Deps, task: TaskState | None, issue: int) -> bool:
    return bool(task is not None
                and task.stage in IN_FLIGHT_STAGES
                and not task.park
                and deps.sessions.is_alive(task.target, issue))


def _apply_park_intent(cfg: Config, deps: Deps, by_name: dict,
                       task: TaskState | None, issue: int) -> None:
    if not _parkable_task(deps, task, issue):
        print(f"[warn] park intent for #{issue}: no live unparked task "
              f"— skipped", file=sys.stderr)
        return
    assert task is not None
    target = by_name.get(task.target)
    if target is None:
        print(f"[warn] park intent for #{issue}: target "
              f"{task.target!r} left the config — skipped", file=sys.stderr)
        return
    _park_for_input(cfg, deps, target, task, note="parked by operator")


def _release_killed_task(cfg: Config, deps: Deps, by_name: dict,
                         task: TaskState, issue: int) -> None:
    target = by_name.get(task.target)
    if target is not None:
        try:
            deps.github.release(target, issue, "abandoned by operator")
        except Exception as exc:
            print(f"[warn] release failed while killing #{issue}: {exc}",
                  file=sys.stderr)
    save(cfg.state_dir, replace(task, stage=Stage.FAILED, park="",
                                hold_for_attach=False, operator_request=None,
                                updated_at=_now()))


def _apply_kill_intent(cfg: Config, deps: Deps, by_name: dict,
                       task: TaskState | None, intent: intents.Intent) -> None:
    issue = intent.issue
    kill_target = intent.target or (task.target if task is not None else "")
    if not kill_target:
        print(f"[warn] kill intent for #{issue}: no unique matching task "
              f"— skipped", file=sys.stderr)
        return
    deps.sessions.end(kill_target, issue)
    if task is not None:
        _release_killed_task(cfg, deps, by_name, task, issue)
    clear_waiting(cfg.state_dir, kill_target, issue)
    eventlog.append_event(cfg.state_dir, "failed", target=kill_target,
                          issue=issue,
                          stage=task.stage.value if task is not None else "",
                          actor=intent.actor, detail="killed by operator")


def _cancel_board_issue(deps: Deps, by_name: dict, target_name: str,
                        issue: int) -> None:
    target = by_name.get(target_name)
    if target is None:
        log.warning("cancel intent for %s/#%d: target left the config "
                    "— board/issue untouched", target_name, issue)
        return
    try:
        deps.github.cancel(target, issue)
    except Exception:
        log.warning("github cancel failed for %s/#%d", target_name, issue,
                    exc_info=True)


def _apply_cancel_intent(cfg: Config, deps: Deps, by_name: dict,
                         task: TaskState | None, intent: intents.Intent) -> None:
    issue = intent.issue
    cancel_target = intent.target or (task.target if task is not None else "")
    if not cancel_target:
        log.warning("cancel intent for #%d: no unique matching task — skipped",
                    issue)
        return
    deps.sessions.end(cancel_target, issue)
    _cancel_board_issue(deps, by_name, cancel_target, issue)
    if task is not None:
        save(cfg.state_dir, replace(task, stage=Stage.CANCELED, park="",
                                    hold_for_attach=False, slot=NO_SLOT,
                                    operator_request=None, updated_at=_now()))
    clear_waiting(cfg.state_dir, cancel_target, issue)
    eventlog.append_event(cfg.state_dir, "canceled", target=cancel_target,
                          issue=issue, stage=task.stage.value if task else "",
                          actor=intent.actor, detail="canceled by operator")


def _apply_retry_intent(cfg: Config, issue: int) -> None:
    for target in cfg.targets:
        qp = failures.quarantine_path(cfg.state_dir, target.name, issue)
        if not qp.exists():
            continue
        try:
            fp = json.loads(qp.read_text()).get("fingerprint")
        except (json.JSONDecodeError, OSError):
            fp = None
        if fp:
            failures.fingerprint_path(cfg.state_dir, fp).unlink(missing_ok=True)
        qp.unlink(missing_ok=True)


class IntentDropped(Exception):
    """An intent the dispatcher refuses: dropped with an event naming why."""

    def __init__(self, detail: str, model: str = ""):
        super().__init__(detail)
        self.model = model


def _require_resume_model(cfg: Config, by_name: dict,
                          task: TaskState, model: str) -> None:
    """The authoritative check of a resume intent's model (an intent file can
    bypass the console's 422): configured for the target, and of the stage
    pick's provider. Raises IntentDropped."""
    if not model:
        return
    target = by_name.get(task.target)
    policy = policy_for(cfg, target) if target else cfg.models
    if model not in policy.model_ids():
        raise IntentDropped(f"model {model!r} is not configured for target "
                            f"{task.target!r}", model)
    refusal = override_refusal(task.picks, next_stage(task), model)
    if refusal:
        raise IntentDropped(refusal, model)


def _save_queued_resume(cfg: Config, task: TaskState, model: str,
                        bypass_usage: bool) -> None:
    save(cfg.state_dir, replace(task, resume_model_override=model,
                                resume_bypass_usage=bypass_usage))


def _is_parked(task: TaskState | None) -> bool:
    return task is not None and bool(task.park)


def _apply_resume_intent(cfg: Config, by_name: dict,
                         task: TaskState | None, intent: intents.Intent) -> None:
    issue = intent.issue
    if task is not None and resumable_crash(task):
        # Back to the stage it crashed in, queued like any wake so capacity,
        # slots and admission still decide when; crashed_stage stays set so
        # _resume_one respawns the stage instead of continuing a transcript.
        task = replace(task, stage=Stage(task.crashed_stage), terminal_at="")
    elif not _is_parked(task):
        print(f"[warn] resume intent for #{issue}: task not parked — skipped",
              file=sys.stderr)
        return
    assert task is not None
    requested_model = str(intent.payload.get("model") or "")
    _require_resume_model(cfg, by_name, task, requested_model)
    bypass_usage = intent.payload.get("bypass_usage") is True
    if task.park == PARK_WAKE:
        _save_queued_resume(cfg, task, requested_model, bypass_usage)
        return
    _wake(cfg, task,
          intent.payload.get("text")
          or "The operator resumed this task. Continue.",
          hold=False, actor=intent.actor or "operator",
          model_override=requested_model, bypass_usage=bypass_usage)


def _apply_one_intent(cfg: Config, deps: Deps, by_name: dict,
                      intent: intents.Intent) -> None:
    """Route one operator intent to its action-specific handler."""
    issue = intent.issue
    task = _task_for_intent(cfg, intent)
    if intent.action == "reply":
        _apply_reply_intent(cfg, deps, task, intent)
    elif intent.action == "park":
        _apply_park_intent(cfg, deps, by_name, task, issue)
    elif intent.action == "kill":
        _apply_kill_intent(cfg, deps, by_name, task, intent)
    elif intent.action == "cancel":
        _apply_cancel_intent(cfg, deps, by_name, task, intent)
    elif intent.action == "retry":
        _apply_retry_intent(cfg, issue)
    elif intent.action == "resume":
        _apply_resume_intent(cfg, by_name, task, intent)
    else:
        print(f"[warn] unknown intent action {intent.action!r} for #{issue}",
              file=sys.stderr)


def _intent_target(cfg: Config, intent: intents.Intent) -> str:
    """The target an intent's event names. A legacy intent (target == "")
    carries no target of its own; if it resolves unambiguously to one task,
    use THAT task's target so the console's /task/{target}/{issue} link is
    never blank. Same fallback idiom as the "kill" action's kill_target."""
    resolved = _task_for_intent(cfg, intent)
    return intent.target or (resolved.target if resolved is not None else "")


def _apply_intents(cfg: Config, deps: Deps) -> None:
    """Drain operator intents (web console writes) at the top of the pass.
    Applied-then-deleted = at-most-once; a failed intent is deleted too,
    noted to stderr, and never aborts the pass or the remaining intents."""
    by_name = {t.name: t for t in cfg.targets}
    for intent in intents.list_intents(cfg.state_dir):
        try:
            _apply_one_intent(cfg, deps, by_name, intent)
            eventlog.append_event(cfg.state_dir, "intent-applied",
                                  target=_intent_target(cfg, intent),
                                  issue=intent.issue, actor=intent.actor,
                                  detail=intent.action)
        except IntentDropped as dropped:
            print(f"[warn] intent {intent.path.name}: {dropped}",
                  file=sys.stderr)
            eventlog.append_event(cfg.state_dir, "intent-dropped",
                                  target=_intent_target(cfg, intent),
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
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "started_at": started_at, "finished_at": _now(),
            "interval_minutes": cfg.pass_interval_minutes}))
        tmp.replace(p)
    except OSError as exc:
        print(f"[warn] heartbeat write failed: {exc}", file=sys.stderr)


def _prune_snapshots(cfg: Config) -> None:
    """A snapshot outlives its task by at most a week. In-flight tasks keep
    theirs regardless of age — the console may need it while they are
    parked. Every pass checks; ~200 KB per file makes eagerness cheap.

    The live set is built from state, not parsed from the stem: a
    "task-<target>-<issue>.txt" stem can't be split back into (target,
    issue) unambiguously (a target name may itself contain digits and
    hyphens), so identity comes from load_all() same as everywhere else,
    same as _task_for_intent. The legacy "task-<issue>.txt" form is kept
    alive too, for any snapshot still sitting under its pre-rename name."""
    root = Path(cfg.state_dir) / "snapshots"
    if not root.exists():
        return
    try:
        in_flight = [t for t in load_all(cfg.state_dir)
                     if t.stage in IN_FLIGHT_STAGES]
    except Exception:
        return  # unreadable/corrupt state dir: skip, never abort the sweep
    live = {f"task-{t.target}-{t.issue}.txt" for t in in_flight} \
         | {f"task-{t.issue}.txt" for t in in_flight}
    now = time.time()
    for p in root.glob("task-*.txt"):
        if p.name in live:
            continue
        try:
            if now - p.stat().st_mtime > SNAPSHOT_TTL_SECONDS:
                p.unlink()
        except OSError:
            continue


@contextmanager
def _sandboxed_state(cfg: Config):
    """Yield `cfg` with state_dir rebound to a throwaway copy of the real one.

    --dry-run stubs every I/O *dependency* (GitHubClient, Sessions, Notifier,
    remove_workspace), but the pass also writes locally — state files, the
    event log, waiting markers, failure reports, the intent queue,
    the per-provider usage cache — and those writes were unguarded, so a dry run advanced
    tasks to terminal stages and wrote history for real. Guarding each call
    site is what already rotted: `dry_run` is threaded by hand into some
    functions and was never added to _flush_done or _resume_woken.

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
    denied? Exactly the two queues that mark the marker: _resume_woken's
    (park == PARK_WAKE) and _spawn_feedback's (a pr-open task with pending
    feedback). Anything else is not waiting for anything."""
    return task.park == PARK_WAKE or task.feedback_pending


def _migrate_legacy_keys(cfg: Config) -> None:
    """Take over pre-multi-project issue-only keys. Messages: see
    messages.migrate_legacy. Wake-blocked markers are transient (re-marked
    next pass if still true), so with several targets a legacy one is just
    dropped; with one it is renamed to keep the edge-triggered event quiet."""
    names = [t.name for t in cfg.targets]
    messages.migrate_legacy(cfg.state_dir, names)
    for p in Path(cfg.state_dir).glob(f"{WAKE_BLOCKED_PREFIX}*"):
        issue = p.name.removeprefix(WAKE_BLOCKED_PREFIX)
        if not issue.isdigit():
            continue
        if len(names) == 1:
            p.replace(_wake_blocked_path(cfg, names[0], int(issue)))
        else:
            p.unlink(missing_ok=True)


def _reconcile_slots(cfg: Config) -> None:
    """Re-establish two invariants from disk at the top of every pass, so no
    terminal path has to remember them and no manual state surgery is ever
    needed.

    Slots: a task that no longer holds its slot by holds_slot() gives the
    number back. That is every session-ending park (the leak older code left
    behind) AND every task that reached a terminal stage — pr-open, done,
    failed — still recording one.

    Wake-blocked markers: the marker (and the "waiting for a free slot"
    badge and delivery_contract suffix it drives) is dropped for every task
    that is not _starving(), plus every marker whose task file is gone. It
    used to be unlinked only on a successful resume/feedback-spawn and in
    _flush_done, so every other terminal transition — the kill intent's
    FAILED tombstone, a crash, a closed PR, a done task inside its retention
    window — left a dead card claiming forever that it was waiting for a
    slot, and a re-claim of the same issue inherited the stale badge."""
    known: set[str] = set()
    for task in load_all(cfg.state_dir):
        known.add(_wake_blocked_path(cfg, task.target, task.issue).name)
        if not holds_slot(task) and task.slot != NO_SLOT:
            eventlog.append_event(cfg.state_dir, "slot-reclaimed",
                                  target=task.target, issue=task.issue,
                                  stage=task.stage.value,
                                  detail=f"stage {task.stage.value}"
                                         + (f" park {task.park}" if task.park
                                            else " unparked")
                                         + f" released slot {task.slot}")
            task = replace(task, slot=NO_SLOT)
            save(cfg.state_dir, task)
        if not _starving(task):
            _clear_wake_blocked(cfg, task.target, task.issue)
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


def _sync_artifacts(cfg: Config, *, dry_run: bool = False) -> None:
    repos = {target.name: target.repo for target in cfg.targets}
    for task in load_all(cfg.state_dir):
        try:
            task_artifacts.collect(cfg.state_dir, task, repos.get(task.target, ""),
                                   publish=not dry_run and task.stage not in TERMINAL_STAGES)
        except (OSError, ValueError):
            log.exception("Artifact collection failed for %s/%s", task.target, task.issue)
    task_artifacts.cleanup(cfg.state_dir)


def _run_pass(cfg: Config, deps: Deps, dry_run: bool = False,
              config_path: str = "targets.yaml") -> None:
    pass_started = _now()
    _migrate_legacy_keys(cfg)
    _reconcile_slots(cfg)
    if not dry_run:
        _apply_intents(cfg, deps)
        _prune_snapshots(cfg)
        triage.tick(cfg, deps, config_path)
    # Evaluate each triage signal once — triage.running() is a real herdr
    # subprocess; calling it per target would spawn (1 + len(targets)) processes
    # each pass. The two signals are semantically distinct: eff is reduced when
    # the sweep is running (it holds a real capacity unit active() cannot see);
    # claims are paused when any request is in flight (pending = file OR running).
    sweep_running = triage.running()
    eff = replace(cfg, capacity=max(0, cfg.capacity - 1)) if sweep_running else cfg
    claims_paused = triage.pending(cfg.state_dir)
    _handle_telegram(cfg, deps, dry_run)
    usages = fetch_all(cfg)
    now = datetime.now(timezone.utc)
    admit: Admit = lambda model: admits(usages, model, now, cfg.pace)
    default_verdict = admit(cfg.models.gate_entry().model_id)
    _budget_edge(cfg, deps, default_verdict, now)
    _auth_dark_edge(cfg, deps, usages)
    # Phase 1: work already in progress, on every target, before any claim —
    # so one target's new claims never starve another's approved spec.
    for target in eff.targets:
        for task in [t for t in load_all(cfg.state_dir)
                     if t.target == target.name and not t.park
                     and t.stage in IN_FLIGHT_STAGES]:
            try:
                _drive_task(eff, deps, target, task, admit, dry_run)
            except Exception:
                _fail_task_crash(eff, deps, target, task, dry_run)
        _wake_ci(eff, deps, target)
        _poll_prs(eff, deps, target, dry_run)
    _resume_woken(eff, deps, admit, dry_run)
    _spawn_feedback(eff, deps, admit)
    # Phase 2: new claims, with whatever capacity phase 1 left.
    if not claims_paused:
        _claim_new(eff, deps, eff.targets, admit, dry_run, pass_started)
    _sync_artifacts(cfg, dry_run=dry_run)
    _flush_done(cfg)
    _write_heartbeat(cfg, pass_started)


def guarded_pass(cfg: Config, deps: Deps, config_path: str,
                 dry_run: bool = False) -> None:
    try:
        run_pass(cfg, deps, dry_run=dry_run, config_path=config_path)
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
    ap.add_argument("--triage", action="store_true")
    ap.add_argument("--triage-run", action="store_true")
    ap.add_argument("--migrate-tmux", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)
    missing = referenced_providers(cfg) - set(ADAPTERS)
    if missing:
        print(f"[warn] models: provider(s) {sorted(missing)} have no usage adapter; "
              f"their entries are never admitted", file=sys.stderr)
    deps = Deps(github=GitHubClient(dry_run=args.dry_run),
                sessions=Sessions(dry_run=args.dry_run, memory=cfg.session_memory, cpus=cfg.session_cpus, state_dir=cfg.state_dir),
                notifier=Notifier(dry_run=args.dry_run,
                                  console_url=cfg.console_url,
                                  multi_target=len(cfg.targets) > 1))
    if args.digest:
        send_digest(cfg, deps)
    elif args.triage:
        if triage.enqueue(cfg.state_dir):
            print("triage request enqueued")
        else:
            print("triage already pending or running; not enqueued")
    elif args.triage_run:
        triage.guarded_sweep(cfg, deps)
    elif args.migrate_tmux:
        # Called by agent-ops-infra/provision/update.sh, which already holds convergence.lock
        # (the same file pass_lock flocks — taking it here again would block
        # forever, flock being per open-file-description). Never run by hand
        # while the dispatcher timer is live.
        for line in tmux_migration.migrate(
                cfg.state_dir, lambda task, text: _wake(cfg, task, text)):
            print(line)
    else:
        with pass_lock(cfg.state_dir):
            guarded_pass(cfg, deps, args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
