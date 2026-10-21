"""One dispatcher pass: candidates → capacity → budget → claim → drive.

Stateless per pass — the view is rebuilt every time from the board (via
candidates), state files, stage.json signals, and session liveness. Run by a
systemd timer; one invocation = one pass."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
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
from enum import Enum
from pathlib import Path
from typing import Callable

from dispatcher.convergence import claim_lock, pass_lock
from dispatcher.config import (Config, Target, apply_stored_models,
                               load_config, policy_for, project_target,
                               referenced_providers, routed_providers, stranded)
from dispatcher.usage import ProviderUsage, Verdict, admits, verdict_note
from dispatcher.usage_providers import ADAPTERS, fetch_all
from dispatcher import (answers, claims, sandbox_lifecycle, eventlog, execution_overrides, failures, intents, loops,
                        messages, pr_poll, priority, queue_ops, relogin, required_pr_commands, state, tmux_migration,
                        triage)
from dispatcher.credential_service import configured_credentials
from dispatcher.github import Candidate, GitHubClient

log = logging.getLogger(__name__)
from dispatcher import spec_publish, task_artifacts
from dispatcher.artifacts import TICKETS_DIR, page_revision, task_plan_revision
from dispatcher.loops import Decision, Outcome, ResetCause
from dispatcher.machine import (ApplyDecision, BackgroundView, DisarmPlanApproval,
                                HandleCrash, NoOp, Notify, ParkForCI,
                                ParkForInput, ParkForReview, PublishSpec,
                                RecordBackgroundWait, Poke, RetryStage, ReviewPark, SetTaskStage,
                                SpawnStage, pass_actions)
from dispatcher.models import (IMPLEMENT_PICK, Admitted, Entry, ModelPolicy,
                               Order, override_refusal,
                               parse_entry, pick_key, second_model,
                               stage_pick, track_from_labels, track_gone,
                               tracks_text)
from dispatcher.prompts import render_stage_prompt
from dispatcher.runtimes import runtime_for
from dispatcher.sandbox_runtime import BoxSandboxes
from dispatcher.sessions import Sessions
from dispatcher.state import (TERMINAL_STAGES, IN_FLIGHT_STAGES, NO_POKES, NO_SLOT, PARK_CI,
                              PARK_HUMAN,
                              PARK_LOGIN, PARK_REVIEW, PARK_WAKE, WAKE_BLOCKED_PREFIX,
                              WHY_CONFIG, WHY_DECISION, WHY_LOOP_CAP, WHY_OPERATOR,
                              RESPAWNABLE_STAGES, AnswersRequest, PlanApprovalRequest,
                              CI_FIX_LIMIT, ScopeRefused, Stage, StageSignal, TaskBundle, TaskRepository, TaskState, active,
                              read_bundle, save_bundle, select_spec_repo, allocate_slot,
                              clear_session, clear_turn_markers, delete, has_waiting,
                              holds_slot, launch_entries, load,
                              load_all, max_slots,
                              next_stage, read_background, read_session,
                              read_stage_signal,
                              implement_done, resumable_crash, shown_stage,
                              save, task_key, owns_task_session, token_issued)
from dispatcher import state
from dispatcher.workspace import (append_worktree_file, create_workspace,
                                  exclude_local_state, remove_workspace,
                                  write_task_identity, write_worktree_file)
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
    credentials: object = None  # ExecutionCredentials; None keeps the host login
    sandbox: object = None      # task sandboxes (sandbox_lifecycle); None leaves them alone
    verification: object = None  # onboarding verification requests (pending/start/is_running)


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
    return policy_for(cfg, target)


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
                stage: Stage, admitted: Admitted,
                order: Order) -> Launch | None:
    """The stage's recorded pick, else the first admitted of the entries the
    launch walks (state.launch_entries). None: its track is not usable, or
    nothing is admitted — wait."""
    pick = stage_pick(task.picks, stage.value)
    if pick:
        return Launch(stage, parse_entry(pick, "pick"))
    entries = launch_entries(task, _policy(cfg, target), order, stage.value)
    entry = next((e for e in entries if admitted(e.model_id)), None)
    return Launch(stage, entry) if entry else None


def _admitted(admit: Admit, bypass: bool) -> Admitted:
    return (lambda m: True) if bypass else (lambda m: admit(m).admitted)


def _with_second(cfg: Config, target: Target | None, launch: Launch,
                 admit: Admit) -> Launch:
    """Grant the second model on the gate itself: a bypass never covers it."""
    second = second_model(_policy(cfg, target), launch.stage.value,
                          launch.entry, _admitted(admit, False))
    return replace(launch, second=second)


def _execution_override_for(cfg: Config, task: TaskState,
                            stage: Stage) -> execution_overrides.ExecutionOverride | None:
    """Revalidate a saved choice against the stage actually about to launch.

    The web's task snapshot can precede a stage transition. Drop an obsolete
    cross-provider choice, including its bypass, before selecting a launch.
    """
    override = execution_overrides.load(cfg.state_dir, task.target, task.issue,
                                        repo=task.repo)
    if override is None or not override.model:
        return override
    refusal = override_refusal(task.picks, stage.value, override.model)
    if refusal:
        execution_overrides.delete(cfg.state_dir, task.target, task.issue,
                                   repo=task.repo)
        eventlog.append_event(cfg.state_dir, "execution-override-dropped",
                              target=task.target, issue=task.issue, repo=task.repo,
                              stage=stage.value, model=override.model, detail=refusal)
        return None
    return override


def _choose_launch(
        cfg: Config, target: Target | None, task: TaskState, stage: Stage,
        admit: Admit, order: Order,
        override: execution_overrides.ExecutionOverride | None,
) -> tuple[Launch | None, bool, str]:
    """What a spawn site launches, whether usage is bypassed, and why a
    denied launch never comes. A one-shot operator `override`
    (_execution_override_for; on a wake also what the resume named) names
    the entry outright; otherwise the sticky pick or the track decides. A
    denied launch is (None, bypass, why): nothing mutated, the signal
    persists, retried once headroom returns; `why` (models.stranded) says
    when it never returns, and the caller parks."""
    bypass = bool(override and override.bypass_usage)
    if override is not None and override.model:
        launch = Launch(stage, parse_entry(override.model, "override"))
        if bypass or admit(launch.model).admitted:
            return _with_second(cfg, target, launch, admit), bypass, ""
        return None, bypass, ""
    launch = _launch_for(cfg, target, task, stage, _admitted(admit, bypass), order)
    if launch is None or (not bypass and not admit(launch.model).admitted):
        return None, bypass, stranded(cfg, target, task, stage.value)
    return _with_second(cfg, target, launch, admit), bypass, ""


def _action_launch(cfg: Config, target: Target, task: TaskState, stage: Stage | None,
                   admit: Admit, order: Order) -> tuple[Launch | None, bool, str]:
    """_choose_launch for the stage an action launches; nothing when it
    launches none."""
    if stage is None:
        return None, False, ""
    return _choose_launch(cfg, target, task, stage, admit, order,
                          _execution_override_for(cfg, task, stage))


def _candidate_launch(cfg: Config, target: Target, cand: Candidate,
                      admit: Admit, order: Order) -> tuple[Launch | None, bool]:
    """An unclaimed candidate has no picks: its spec entry comes from the
    track its labels name (else the untracked track)."""
    policy = policy_for(cfg, target)
    probe = TaskState(issue=cand.number, target=target.name, stage=Stage.QUEUED,
                      repo=cand.repo, slot=NO_SLOT, worktree="", branch="", title=cand.title,
                      updated_at="", track=track_from_labels(cand.labels, policy))
    launch, bypass, _ = _choose_launch(cfg, target, probe, Stage.SPEC, admit, order,
                                       _execution_override_for(cfg, probe, Stage.SPEC))
    return launch, bypass   # no picks and a configured track: nothing strands it


def _consume_execution_choice(cfg: Config, target: str, issue: int,
                              repo: str = "") -> None:
    execution_overrides.delete(cfg.state_dir, target, issue, repo=repo)


def _display_entry(cfg: Config, target: Target | None, task: TaskState) -> str:
    """The entry a task runs or would run next, for status lines: its pick for
    the next launch, else the first of the entries that launch walks
    (state.launch_entries), else ''. No usage reading here, so the order is
    built from an empty one: a fixed mode applies, and in auto every entry
    is unrated, which is the written order."""
    pick = stage_pick(task.picks, next_stage(task))
    if pick:
        return pick
    entries = launch_entries(task, _policy(cfg, target), priority.order(
        priority.load(cfg.state_dir, routed_providers(cfg)), {},
        datetime.now(timezone.utc), cfg.pace))
    return str(entries[0]) if entries else ""


def _last_launched(worktree: str) -> str:
    """The entry of the worktree's last launch, from the models.log
    breadcrumb; "" when there is none."""
    p = Path(worktree) / ".agent" / "models.log"
    lines = p.read_text().splitlines() if p.exists() else []
    return lines[-1].split()[-1] if lines else ""


def _log_model(worktree: str, stage: Stage, model: str) -> None:
    """Durable per-worktree breadcrumb. stage.json is co-owned — sessions
    overwrite it when they signal — so the log is the record that survives."""
    append_worktree_file(worktree, ".agent", "models.log",
                         f"{_now()} {stage.value} {model}\n")


def _url(target: Target, issue: int) -> str:
    return f"https://github.com/{target.repo}/issues/{issue}"


def _queue_message(cfg: Config, target: str, issue: int, text: str,
                   actor: str, repo: str = "") -> None:
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
    messages.append(cfg.state_dir, target, issue, text, actor, repo=repo)


def _message_block(msgs: list[messages.Message]) -> str:
    """The block appended to a spawn/resume prompt, oldest first. Actor and
    timestamp are included so the agent can tell an operator instruction from
    a dispatcher-authored CI note."""
    if not msgs:
        return ""
    lines = ["## Operator messages", ""]
    lines += [f"- [{m.created_at}] {m.actor}: {m.text}" for m in msgs]
    return "\n".join(lines)


def _drain(cfg: Config, target: str, issue: int,
           repo: str = "") -> tuple[str, list[str]]:
    """(block, ids) for everything queued on this issue. The caller stamps
    the ids only AFTER the session actually took the prompt, so a spawn that
    raises leaves the mail queued for the next attempt. Ids are captured
    here, not re-derived later, so a message that arrives during the spawn is
    not stamped as delivered without ever being shown."""
    msgs = messages.undelivered(cfg.state_dir, target, issue, repo=repo)
    return _message_block(msgs), [m.id for m in msgs]


def _wake(cfg: Config, task: TaskState, text: str, hold: bool = False,
          actor: str = "dispatcher", *, model_override: str = "",
          bypass_usage: bool = False) -> None:
    _queue_message(cfg, task.target, task.issue, text, actor, task.repo)
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

    Un-parking then restores the same invariant _resume_woken and _retry_stage
    enforce — no waiting marker, a `working` signal — or the very next pass
    re-reads blocked/awaiting-ci, re-parks, and ENDS the session that was
    just re-authenticated."""
    if relogin.classify_login(
            deps.sessions.capture_tail(task.target, task.issue, **_repo_kw(task))) is None:
        deps.notifier.send("status", lines=[
            f"{_ref(cfg, task)} is no longer at a login prompt — code NOT typed "
            f"(the pane would have run it as a shell command). Still parked; "
            f"attach to the session to sort it out."])
        return
    deps.sessions.send_text(task.target, task.issue, code.strip(), **_repo_kw(task))
    clear_turn_markers(cfg.state_dir, task.target, task.issue, repo=task.repo)
    signal = read_stage_signal(task.worktree)
    if signal is not None and signal.status != "working":
        by_name = {t.name: t for t in cfg.targets}
        model = _display_entry(cfg, by_name.get(task.target), task)
        try:
            write_worktree_file(task.worktree, ".agent", "stage.json", json.dumps(
                {"stage": task.stage.value, "status": "working", "model": model}))
        except OSError as exc:
            # Not written: the next pass reads the old signal and parks again.
            print(f"[warn] stage signal of #{task.issue} not rewritten: {exc}",
                  file=sys.stderr)
    save(cfg.state_dir, replace(task, park="", park_msg_id=0,
                                park_note="", updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "login-code-injected",
                          target=task.target, issue=task.issue, repo=task.repo,
                          stage=task.stage.value, detail="login code injected")


def _status_lines(cfg: Config) -> list[str]:
    by_name = {t.name: t for t in cfg.targets}
    tasks = [t for t in load_all(cfg.state_dir) if t.stage in IN_FLIGHT_STAGES]
    lines = []
    for t in tasks:
        model = _display_entry(cfg, by_name.get(t.target), t)
        slot = "(no slot)" if t.slot == NO_SLOT else f"(slot {t.slot})"
        lines.append(f"{_ref(cfg, t)} {t.title} — {shown_stage(t)} [{model}]"
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


def _find_rows(cfg: Config, deps: Deps, issue: int, repo: str = "") -> list[tuple]:
    return [(target, row)
            for target in cfg.targets
            for row in deps.github.rank_rows(target)
            if row["number"] == issue and repo in ("", row.get("repo", ""))]


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


def _locate(cfg: Config, deps: Deps, issue: int, repo: str = ""):
    """Resolve an issue (of `repo`, when named) to (target, row); reports and
    returns None when it can't."""
    hits = _find_rows(cfg, deps, issue, repo)
    if not hits:
        deps.notifier.send("status", lines=[f"#{issue} is not on the board"])
        return None
    if len(hits) > 1:
        deps.notifier.send("status", lines=[
            f"#{issue} exists in multiple targets ("
            + ", ".join(f"{row['repo']}#{issue}" if row.get("repo") else t.name
                        for t, row in hits)
            + ") — name one as owner/name#" + str(issue)])
        return None
    return hits[0]


def _handle_boost(cfg: Config, deps: Deps, issue: int, amount: int,
                  repo: str = "") -> None:
    located = _locate(cfg, deps, issue, repo)
    if not located:
        return
    target, row = located
    plan = queue_ops.plan_boost(row, amount)
    queue_ops.apply_plan(deps.github, target, issue, plan, row.get('repo', ''))
    deps.notifier.send("status", lines=plan.reason.split("\n"))


def _handle_next(cfg: Config, deps: Deps, issue: int, force: bool,
                 repo: str = "") -> None:
    located = _locate(cfg, deps, issue, repo)
    if not located:
        return
    target, row = located
    plan = queue_ops.plan_next(row, force)
    queue_ops.apply_plan(deps.github, target, issue, plan, row.get('repo', ''))
    deps.notifier.send("status", lines=plan.reason.split("\n"))


# A failing gh or rank call answers in chat instead of crashing the pass.
_COMMAND_ERRORS = (subprocess.CalledProcessError, OSError, LookupError,
                   ValueError)


def _reply_parked(tasks: list[TaskState]) -> list[TaskState]:
    # Both parks answer the same way — a reply is text for the session. They
    # stay distinct kinds so /status and the board can say "needs input
    # mid-stage" vs "plan ready, review at leisure".
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
        _handle_attach(cfg, deps, tasks, ev.issue, ev.repo)
    else:
        _handle_board_command(cfg, deps, ev)


def _handle_attach(cfg: Config, deps: Deps, tasks: list[TaskState],
                   issue: int, repo: str = "") -> None:
    """A bare number names one task only while no other target or repository
    has the same number parked; when two do, wake nothing and list them."""
    match = [t for t in tasks if t.issue == issue and t.park
             and repo in ("", t.repo)]
    if len(match) == 1:
        _wake(cfg, match[0], "", hold=True)
    elif match:
        deps.notifier.send("status", lines=(
            [f"#{issue} is ambiguous; parked tasks with that number:"]
            + [f"{t.repo}#{t.issue}" if t.repo else _ref(cfg, t)
               for t in match]))
    else:
        deps.notifier.send("status", lines=[f"#{issue} is not parked"])


def _handle_board_command(cfg: Config, deps: Deps, ev: Command) -> None:
    """/queue, /boost and /next."""
    try:
        if ev.name == "queue":
            deps.notifier.send("queue", lines=_queue_lines(cfg, deps))
        elif ev.name == "boost":
            _handle_boost(cfg, deps, ev.issue, ev.amount, ev.repo)
        elif ev.name == "next":
            _handle_next(cfg, deps, ev.issue, ev.force, ev.repo)
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
    deps.notifier.send(template, issue=task.issue, repo=task.repo, title=task.title,
                       url=_url(target, task.issue), note=note,
                       target=target.name)


def _heal_identity(task: TaskState) -> None:
    """Before a launch or a resume: .agent/task.json is rewritten from the
    task's own state, whatever the file held. A failure is logged and the
    launch goes on."""
    try:
        write_task_identity(task.worktree, task.issue, task.target, task.branch)
    except OSError as exc:
        print(f"[warn] task identity not written for #{task.issue}: {exc}",
              file=sys.stderr)


def _heal_exclude(target: Target, task: TaskState) -> None:
    """Before a launch or a resume: a worktree made before the exclude lines
    existed gets them now. The path is the target's configured clone, never
    one taken from the worktree. The lines are a protection, not a
    precondition for a task that already exists: a failure is logged and the
    launch goes on."""
    try:
        exclude_local_state(target.clone_path)
    except OSError as exc:
        print(f"[warn] exclude lines not written for #{task.issue}: {exc}",
              file=sys.stderr)


def _write_working(worktree: str, stage: Stage, entry) -> None:
    """Rewrite .agent/stage.json to `working` BEFORE a session is launched or
    resumed, or the next pass reads the old signal again (blocked, a rejected
    report, the previous stage's `done`) and acts on it a second time. An
    implement session's progress note is kept for the same stage: the
    tickets it counts are still merged on the branch, and the console shows
    them until the session reports again. Never across stages. Raises OSError
    when .agent is not a usable directory: the launch then fails with that
    reason, and nothing is written outside the worktree."""
    doc = {"stage": stage.value, "status": "working", "model": entry.model_id,
           "effort": entry.effort}
    old = read_stage_signal(worktree)
    if (stage is Stage.IMPLEMENT and old is not None and old.note
            and (old.stage, old.status) == (stage.value, "working")):
        doc["note"] = old.note
    write_worktree_file(worktree, ".agent", "stage.json", json.dumps(doc))
    _log_model(worktree, stage, str(entry))


def _with_implement_provider(task: TaskState, stage: Stage, entry) -> list[str]:
    """The providers that ran implement, with `entry`'s when it launches an
    implement session: review reads them (models.review_avoid)."""
    providers = task.implement_providers
    if stage is Stage.IMPLEMENT and entry.provider not in providers:
        providers = [*providers, entry.provider]
    return providers


def _spawn_stage(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 launch: Launch, spec_path: str = "") -> TaskState:
    # Logical stage boundaries reset loop budgets; a replacement conversation
    # calls the same launch I/O without creating a new allowance.
    task = loops.reset(task, ResetCause.STAGE_STARTED)
    return _launch_stage(cfg, deps, target, task, launch, spec_path)


def _session_checkout(task: TaskState, member: TaskRepository) -> str:
    """Secondary checkouts are mounted under /workspaces in session containers."""
    return (task.worktree if member.worktree == task.worktree
            else f"/workspaces/{member.repo.replace('/', '~')}")


def _awaits_spec_repo(cfg: Config, task: TaskState, stage: Stage) -> TaskBundle | None:
    """The task's scope while a spec stage waits for its spec/plan repository."""
    bundle = read_bundle(cfg.state_dir, task.target, task.issue, repo=task.repo)
    return bundle if stage is Stage.SPEC and bundle and bundle.awaits_spec_repo else None


def _held_for_spec_repo(cfg: Config, deps: Deps, target: Target, task: TaskState,
                        stage: Stage) -> bool:
    """Park a spec launch until the operator chooses the spec/plan repository,
    so no session writes or pushes the spec anywhere else first."""
    bundle = _awaits_spec_repo(cfg, task, stage)
    if bundle is None:
        return False
    choices = ", ".join(r.repo for r in bundle.repositories)
    _park_for_input(cfg, deps, target, replace(task, stage=stage),
                    f"Choose the spec/plan repository ({choices}) in the console: "
                    "the spec stage starts there once it is chosen.")
    return True


def _session_plan_checkout(cfg: Config, task: TaskState) -> str:
    bundle = read_bundle(cfg.state_dir, task.target, task.issue, repo=task.repo)
    if bundle and bundle.spec_repo:
        member = next(r for r in bundle.repositories if r.repo == bundle.spec_repo)
        return _session_checkout(task, member)
    return task.worktree


def _repository_context(cfg: Config, task: TaskState) -> str:
    bundle = read_bundle(cfg.state_dir, task.target, task.issue, repo=task.repo)
    if bundle is None:
        owner = state.pending_spec_repo(cfg.state_dir, task.target, task.issue, repo=task.repo)
        return (f"The definition selects {owner} as the spec/plan repository. "
                "Its checkout is provisioned after exploration. Keep the explored spec local; "
                "override the spec stage's push requirement and report done after writing it. "
                "The controller transfers it to the selected checkout at handover; "
                "the plan stage commits and pushes it there.") if owner else ""
    checkouts = ", ".join(f"{r.repo}: {_session_checkout(task, r)}" for r in bundle.repositories)
    root = _session_plan_checkout(cfg, task)
    return (f"Repository checkouts: {checkouts}. Spec/plan repository: {bundle.spec_repo or 'awaiting operator selection'}. "
            f"Write, commit and push spec files and tickets in {root}, on {task.branch}. "
            f"Use {root}/{TICKETS_DIR} for ticket execution. "
            f"Keep stage.json, questionnaires, answers and the review page under {task.worktree}/.agent; "
            "their relative paths in this prompt refer to that session control directory. "
            "Report spec artifacts relative to the spec/plan checkout.")


def _repair_identity(task: TaskState, stage: Stage) -> dict:
    """A repair round names the repository-qualified PR it fixes, not the origin's."""
    key = (task.repair_prs or (task.feedback_pr,))[0]
    if stage is not Stage.ADDRESS_REVIEW or not key:
        return {}
    repo, _, number = key.partition("#")
    return {"repo": repo, "pr_number": number}


def _launch_stage(cfg: Config, deps: Deps, target: Target, task: TaskState,
                  launch: Launch, spec_path: str = "",
                  message: str = "") -> TaskState:
    if _session_launch_blocked(cfg, deps, task):
        raise RuntimeError("session ownership is not isolated")
    stage, entry = launch.stage, launch.entry
    model = entry.model_id
    ctx = dict(
        issue_number=task.issue, issue_title=task.title,
        issue_url=_url(target, task.issue), repo=target.repo,
        branch=task.branch, slot=task.slot,
        backend_port=8100 + task.slot, frontend_port=5200 + task.slot,
        verify_cmd=target.verify_cmd.format(slot=task.slot),
        gate_cmd=target.gate_cmd.format(slot=task.slot),
        spec_path=spec_path or task.spec_path,
        tickets_dir=TICKETS_DIR, ticket_count=task.ticket_count,
        pr_number=task.pr_number,
        reason=task.attention or "feedback",
        labels=", ".join(task.labels),
        tracks=tracks_text(_policy(cfg, target)),
    )
    ctx.update(_repair_identity(task, stage))
    planning_root = _session_plan_checkout(cfg, task)
    if planning_root != task.worktree:
        ctx["spec_path"] = str(Path(planning_root) / ctx["spec_path"]) if ctx["spec_path"] else ""
        ctx["tickets_dir"] = str(Path(planning_root) / TICKETS_DIR)
    prompt = render_stage_prompt(stage, ctx)
    repository_context = _repository_context(cfg, task)
    if repository_context:
        prompt += "\n\n" + repository_context
    if stage is Stage.SPEC and target.sandbox:
        prompt += "\n\nProject repository members: " + ", ".join(
            sorted(_project_members(cfg).get(task.target, ())))
    if stage is Stage.ADDRESS_REVIEW and len(task.repair_prs) > 1:
        prompt += "\n\nPull requests to repair: " + ", ".join(task.repair_prs)
    if message:
        prompt = f"{prompt}\n\n{message}"
    block, drained = _drain(cfg, task.target, task.issue, task.repo)
    if block:
        prompt = f"{prompt}\n\n{block}\n"
    _heal_exclude(target, task)
    _heal_identity(task)
    _write_working(task.worktree, stage, entry)
    deps.sessions.spawn_stage(task.target, task.issue, task.worktree, prompt,
                              stage.value, model, entry.effort,
                              second=launch.second, **_repo_kw(task))
    messages.mark_delivered(cfg.state_dir, task.target, task.issue, drained,
                            repo=task.repo)
    task = replace(task, **_launched_stage(task, stage),
                   spec_path=spec_path or task.spec_path,
                   operator_request=None, repair_launch_reserved=False, **NO_POKES,
                   updated_at=_now(),
                   picks={**task.picks, pick_key(stage.value): str(entry)},
                   implement_providers=_with_implement_provider(task, stage, entry))
    save(cfg.state_dir, task)
    eventlog.append_event(cfg.state_dir, "stage-started", target=target.name,
                          issue=task.issue, repo=task.repo, stage=stage.value, model=str(entry))
    return task


def _launched_stage(task: TaskState, stage: Stage) -> dict[str, object]:
    """The stage a launch records. A gated task's plan session runs at the
    gate, so its `done` is the approval."""
    kept = Stage.AWAITING_PLAN_REVIEW if stage is Stage.PLAN and task.gated else stage
    return {"stage": kept,
            "gate_respawn_pending": (task.gate_respawn_pending
                                     and kept is Stage.AWAITING_PLAN_REVIEW)}


def _continue_or_restart(cfg: Config, deps: Deps, target: Target,
                         task: TaskState, launch: Launch,
                         message: str = "") -> tuple[TaskState, str]:
    """Every continuation names its recorded conversation or launches afresh."""
    stage = task.continued_stage
    record = read_session(cfg.state_dir, task.target, task.issue, repo=task.repo)
    detail = ""
    if record is not None and record.stage == stage.value:
        _heal_identity(task)
        block, drained = _drain(cfg, task.target, task.issue, task.repo)
        text = "\n\n".join(filter(None, (message, block, _repository_context(cfg, task)))) or "Continue."
        if _session_launch_blocked(cfg, deps, task):
            raise RuntimeError("session ownership is not isolated")
        deps.sessions.resume(task.target, task.issue, task.worktree, text,
                             launch.model, launch.entry.effort,
                             second=launch.second, session_id=record.session_id,
                             **_repo_kw(task))
        messages.mark_delivered(cfg.state_dir, task.target, task.issue, drained,
                                repo=task.repo)
        task = replace(task, **NO_POKES)
    else:
        detail = (f"new conversation: session recorded for {record.stage}" if record
                  else "new conversation: no session recorded")
        task = _launch_stage(cfg, deps, target, task, replace(launch, stage=stage),
                             message=message or "Continue.")
    eventlog.append_event(cfg.state_dir, "resumed", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
                          model=str(launch.entry), detail=detail)
    return task, detail


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


def _repo_kw(task) -> dict:
    """`repo=` for a session call, only when the task's repository is proven:
    an unqualified task keeps the pre-qualification call shape."""
    return {"repo": task.repo} if task.repo else {}


def _end_session(cfg: Config, deps: Deps, target: str, issue: int,
                 repo: str = "") -> None:
    """End the task's session and drop its turn-end markers (waiting and
    background): the next session must never inherit the old one's. The
    repository names the snapshot the console reads back as this task's."""
    deps.sessions.end(target, issue, **({"repo": repo} if repo else {}))
    clear_turn_markers(cfg.state_dir, target, issue, repo=repo)


def _session_launch_blocked(cfg: Config, deps: Deps, task: TaskState) -> bool:
    """A live session of this task blocks a launch. Its label is its own
    (qualified, or the legacy one it provably owns), so a sibling
    repository's session of the same number never does."""
    return bool(task.repo) and deps.sessions.is_alive(
        task.target, task.issue, **_repo_kw(task))


def _park_answers_request(task: TaskState, artifact: str) -> AnswersRequest | None:
    """Resolve an answer page inside this task's worktree, with its revision."""
    if not artifact:
        return None
    path = Path(artifact)
    resolved = path if path.is_absolute() else Path(task.worktree) / path
    try:
        relative = str(resolved.resolve().relative_to(Path(task.worktree).resolve()))
        fingerprint, problem = page_revision(str(resolved))
        if problem:
            print(f"[warn] #{task.issue} answers page {relative}: {problem}",
                  file=sys.stderr)
        return AnswersRequest(path=relative, fingerprint=fingerprint)
    except ValueError:
        # Path escapes the worktree — treat as unusable reference.
        return None


def _park_for_input(cfg: Config, deps: Deps, target: Target, task: TaskState,
                    note: str, artifact: str = "",
                    is_answers: bool = False,
                    reason: str = WHY_DECISION) -> None:
    target = replace(target, repo=task.repo or target.repo)
    tail = deps.sessions.capture_tail(task.target, task.issue, **_repo_kw(task))
    login = relogin.classify_login(tail)
    if login is not None and _park_for_login(cfg, deps, target, task, note,
                                             tail, login):
        return
    answers_request = _park_answers_request(task, artifact)
    if answers_request is not None:
        answers.discard_stale(task.worktree, answers_request.kind,
                              answers_request.fingerprint)
    if is_answers and answers_request is None:
        note = (note + "\n\n[malformed awaiting-answers: no usable artifact path]").strip()
    msg_id = deps.notifier.send(
        "parked_question", issue=task.issue, repo=task.repo, title=task.title,
        url=_url(target, task.issue), target=target.name,
        note=(note + ("\n\n" + tail if tail else "")).strip() or "(no detail)")
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    # A wake that ends here is over: the wake that follows this park says
    # for itself whether it is an attach, and on which model.
    save(cfg.state_dir, replace(task, park=PARK_HUMAN, park_msg_id=msg_id,
                                park_note=note, park_reason=reason,
                                slot=NO_SLOT, operator_request=answers_request,
                                hold_for_attach=False,
                                resume_model_override="",
                                resume_bypass_usage=False,
                                asked=task.asked or (is_answers and task.stage in (Stage.SPEC, Stage.PLAN)),
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value, detail=note)


def _park_exhausted(cfg: Config, deps: Deps, target: Target, task: TaskState,
                    note: str) -> None:
    """A bounded loop hit its cap with no live session to end (a CI park, or
    a pr-open task): park for the operator. Their reply wakes it with a
    fresh budget (_wake / the reply intent zero the counters)."""
    msg_id = deps.notifier.send(
        "parked_question", issue=task.issue, repo=task.repo, title=task.title,
        url=_url(target, task.issue), target=target.name, note=note)
    save(cfg.state_dir, replace(task, park=PARK_HUMAN, park_msg_id=msg_id,
                                park_note=note, park_reason=WHY_LOOP_CAP,
                                slot=NO_SLOT, ci_run_id=0, operator_request=None,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
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
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
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
        "needs_relogin", issue=task.issue, repo=task.repo, title=task.title,
        url=_url(target, task.issue), target=target.name,
        login_url=login.url or "(attach to the session to see the URL)",
        note=(note + ("\n\n" + tail if tail else "")).strip() or "(no detail)")
    if msg_id == 0:
        return False
    clear_turn_markers(cfg.state_dir, task.target, task.issue, repo=task.repo)
    save(cfg.state_dir, replace(task, park=PARK_LOGIN, park_msg_id=msg_id,
                                park_note=note, operator_request=None,
                                **NO_POKES,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
                          detail="needs re-login: " + note)
    return True


# What a resumed session is told to do about its rejected signal.
_RETRY_HINT = {
    Stage.SPEC: ("Re-write the signal to fix that (the track list, with each "
                 "track's meaning, is in your stage prompt)."),
    Stage.PLAN: ("Fix that in place, then re-write the signal (the signals, "
                 "and the track list with each track's meaning, are in your "
                 "stage prompt). The ticket set under .agent/tickets/ must be "
                 "files named NN-slug.md numbered contiguously from 01 with no "
                 "gaps or duplicates, each with a 'What to build' section, a "
                 "'Blocked by' line and at least one unchecked '- [ ]' "
                 "criterion. Do not re-plan from scratch."),
}


def _retry_stage(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 launch: Launch, act: RetryStage) -> None:
    """Resume the spec or plan session with why its signal was rejected (a
    track that is not configured, a review the stage does not have, the
    ticket format check, a `done` nobody approved), in place, rather than
    failing the task. The resume reads the transcript from the runtime's
    mounted home, so context survives ending the (zombie) session first —
    which we must do, or _launch would type the resume command INTO the
    stopped session's input box (same failure mode as spawning over a live
    session)."""
    entry = launch.entry
    # Before resuming: the next pass must not re-read the rejected signal,
    # re-check the still-unfixed work, and burn the retry immediately.
    _write_working(task.worktree, act.stage, entry)
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    text = (f"Your .agent/stage.json was rejected: {act.reason}. "
            f"{_RETRY_HINT[act.stage]}")
    task, _ = _continue_or_restart(cfg, deps, target, task, launch, text)
    # A rejected plan is no longer on offer: the session's next ready report
    # is checked and armed again.
    save(cfg.state_dir, replace(loops.spend_retry(task, act.retry),
                                updated_at=_now(), operator_request=None))
    if act.stage is Stage.PLAN:
        _notify(deps, target, task, "plan_retry", act.reason)


def _park_for_ci(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 run_id: int) -> None:
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    save(cfg.state_dir, replace(task, park=PARK_CI, ci_run_id=run_id,
                                slot=NO_SLOT, operator_request=None,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
                          detail=f"awaiting CI run {run_id}")


def _grace_elapsed(cfg: Config, task: TaskState) -> bool:
    """Has the plan-review gate gone unanswered past the grace period?
    `updated_at` was stamped when the stage flipped to AWAITING_PLAN_REVIEW,
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


def _publish_spec(cfg: Config, task: TaskState, target: Target,
                  dry_run: bool) -> "spec_publish.PublishResult | None":
    """Publish the spec from the repository that holds it: the bundle's selected
    spec repository, else the task's own. None while a bundle awaits that selection."""
    bundle = read_bundle(cfg.state_dir, task.target, task.issue, repo=task.repo)
    if bundle is None:
        worktree, branch, repo = task.worktree, task.branch, target.repo
    elif not bundle.spec_repo:
        return None
    else:
        member = next(r for r in bundle.repositories if r.repo == bundle.spec_repo)
        worktree, branch, repo = member.worktree, member.branch, member.repo
    return spec_publish.ensure_published(
        worktree=worktree, branch=branch, repo=repo, issue=task.issue,
        artifact=spec_publish.relative_artifact(task.worktree, task.spec_path) or task.spec_path,
        dry_run=dry_run)


def _park_for_review(cfg: Config, deps: Deps, target: Target, task: TaskState,
                     reason: ReviewPark, dry_run: bool = False) -> None:
    """Park a task at the plan gate for a human to read whenever they wake
    up. The only park that also releases the SLOT: the plan stage never used
    the slot's ports and worktrees are per-issue, so resume can take any free
    slot — and freeing it is the whole point, since a held slot would cap the
    overnight run at max_slots(capacity) plans. A session that died with the
    unattended rounds used up may have left no plan: the operator is told
    that, not "plan ready"."""
    died = reason is ReviewPark.SESSION_DIED
    tail = deps.sessions.capture_tail(task.target, task.issue, **_repo_kw(task))
    note = tail.strip() or "(no detail)"
    if task.spec_path:
        pub = _publish_spec(cfg, task, target, dry_run)
        note += f"\n{_spec_note(pub)}" if pub else "\nspec: waiting for its repository to be selected"
    # No msg_id == 0 guard here (unlike _park_for_login): if the ping fails,
    # the task is not stranded — the session is ended and the operator can still
    # reach it via /attach, the console `resume` intent, or plain-text wakes.
    # This matches the same reasoning in _park_for_input.
    msg_id = deps.notifier.send(
        "plan_session_stopped" if died else "plan_parked",
        issue=task.issue, repo=task.repo, title=task.title, url=_url(target, task.issue),
        note=note, target=target.name, rounds=task.unattended_rounds)
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    save(cfg.state_dir, replace(
        task, park=PARK_REVIEW, park_msg_id=msg_id, slot=NO_SLOT, updated_at=_now(),
        park_note=("plan session stopped at the review gate" if died
                   else "plan ready for review")))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
                          detail=reason.value)


def _wake_ci(cfg: Config, deps: Deps, target: Target) -> None:
    for task in load_all(cfg.state_dir):
        if task.target != target.name or task.park != PARK_CI:
            continue
        run_target = _task_target(cfg, target, task)
        try:
            conclusion = deps.github.run_status(run_target, task.ci_run_id)
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
                cfg, deps, run_target, task, dec,
                park_exhausted=lambda t, note: _park_exhausted(cfg, deps, run_target, t, note))
            if parked:
                continue
            if task.stage is Stage.ADDRESS_REVIEW:
                task = replace(task, ci_repair_pending=True, attention="check-failed",
                               repair_prs=task.repair_prs or tuple(filter(None, (task.feedback_pr,))))
        reply = (f"E2E run {task.ci_run_id} concluded: {conclusion} — "
                 f"fetch logs with: gh run view {task.ci_run_id} --log-failed")
        _queue_message(cfg, task.target, task.issue, reply, "dispatcher",
                       task.repo)
        save(cfg.state_dir, replace(task, park=PARK_WAKE, ci_run_id=0,
                                    updated_at=_now()))


def _poll_pr(cfg: Config, deps: Deps, target: Target, task: TaskState
             ) -> "tuple[TaskState, pr_poll.PollResult] | None":
    """One pr-open task's PR, classified; None when it has no PR yet.
    Resolves and saves the PR number on first sight."""
    if not task.pr_number:
        n = deps.github.pr_number_for_branch(target, task.branch)
        if not n:
            print(f"[warn] #{task.issue}: no PR found for branch "
                  f"{task.branch}", file=sys.stderr)
            return None
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
    return task, res


def _poll_prs(cfg: Config, deps: Deps, target: Target,
              dry_run: bool = False) -> None:
    """Watch every pr-open task's PR independently of CI access and capacity.
    Reaction (address-review spawn) is gated
    separately in _spawn_feedback."""
    for task in load_all(cfg.state_dir):
        if task.target != target.name or task.stage is not Stage.PR_OPEN:
            continue
        run_target = _task_target(cfg, target, task)
        task = _discover_required(cfg, deps, run_target, task)
        if _poll_bundle(cfg, deps, run_target, task, dry_run):
            continue
        try:
            polled = _poll_pr(cfg, deps, run_target, task)
            if polled is None:
                continue
            task, res = polled
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[warn] PR poll failed for #{task.issue}: {exc}",
                  file=sys.stderr)
            continue
        if res.kind == "merged":
            try:
                _finish_merged(cfg, deps, run_target, task, dry_run)
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
            _end_session(cfg, deps, task.target, task.issue, task.repo)
            save(cfg.state_dir, replace(task, stage=Stage.FAILED,
                                        operator_request=None,
                                        updated_at=_now()))
            eventlog.append_event(cfg.state_dir, "pr-closed",
                                  target=target.name, issue=task.issue, repo=task.repo,
                                  stage=Stage.PR_OPEN.value,
                                  detail=f"PR #{task.pr_number} closed unmerged")
            _notify(deps, run_target, task, "pr_closed",
                    f"PR #{task.pr_number}. Worktree preserved for autopsy.")
        elif task.park:
            continue   # parked: only merge/close still matter
        elif res.kind == "feedback" and not task.feedback_pending:
            task = loops.reset(task, ResetCause.PR_CYCLE_STARTED)
            save(cfg.state_dir, replace(task, feedback_pending=True,
                                        attention="feedback",
                                        updated_at=_now()))
            eventlog.append_event(cfg.state_dir, "pr-feedback",
                                  target=target.name, issue=task.issue, repo=task.repo,
                                  stage=Stage.PR_OPEN.value,
                                  detail=f"newest {res.latest_ts}")
            _notify(deps, run_target, task, "pr_feedback",
                    f"PR #{task.pr_number}, newest feedback {res.latest_ts}")
        elif res.kind in ("check-failed", "conflict") and not task.feedback_pending:
            _pr_attention(cfg, deps, run_target, task, res)


def _poll_bundle(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 dry_run: bool) -> bool:
    """True when the task is bundled over several repositories and so never takes
    the single-PR poll. A bundle whose PRs are not all discoverable yet polls
    nothing: no one PR may finish the task."""
    if task.required_prs:
        _poll_required(cfg, deps, target, task, dry_run)
    return bool(task.required_prs) or _is_bundled(cfg, task)


def _is_bundled(cfg: Config, task: TaskState) -> bool:
    bundle = read_bundle(cfg.state_dir, task.target, task.issue, repo=task.repo)
    return bundle is not None and len(bundle.repositories) >= 2


def _find_prs(deps: Deps, target: Target, repositories) -> list:
    """A PR per repository, or [] when any has none yet or cannot be looked up."""
    found = []
    for r in repositories:
        try:
            n = deps.github.pr_number_for_branch(replace(target, repo=r.repo), r.branch)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[warn] PR lookup failed in {r.repo}: {exc}", file=sys.stderr)
            return []
        if not n:
            return []
        found.append(state.RequiredPR(r.repo, n))
    return found


def _discover_required(cfg: Config, deps: Deps, target: Target,
                       task: TaskState) -> TaskState:
    """A task bundled over several repositories needs one PR per repository:
    the set is built from the bundle and re-built when the scope widens. It
    grows only once every missing repository has its PR (a half-known set
    would let the task finish early); a single-repository task has no bundle
    and keeps the one-PR poll."""
    bundle = read_bundle(cfg.state_dir, task.target, task.issue, repo=task.repo)
    if not _is_bundled(cfg, task):
        return task
    held = {p.repo for p in task.required_prs} | set(task.dropped_pr_repos)
    found = _find_prs(deps, target, [r for r in bundle.repositories if r.repo not in held])
    if not found:
        return task
    task = replace(task, required_prs=task.required_prs + tuple(
                       _inherit_scalar_pr(task, pr) for pr in found),
                   required_set_revision=_discovered_revision(task, bundle), updated_at=_now())
    save(cfg.state_dir, task)
    return task


def _discovered_revision(task: TaskState, bundle: TaskBundle) -> int:
    previous = task.required_set_revision + bool(task.required_prs)
    return max(previous, bundle.scope_revision)


def _inherit_scalar_pr(task: TaskState, pr: state.RequiredPR) -> state.RequiredPR:
    if (pr.repo, pr.number) != (task.repo, task.pr_number):
        return pr
    return replace(pr, ci_fix_attempts=task.ci_fix_attempts,
                   check_cursor=task.check_cursor, conflict_cursor=task.conflict_cursor,
                   feedback_cursor=task.feedback_cursor, feedback_rounds=task.review_rounds)


def _observe_required(deps: Deps, target: Target, task: TaskState, login: str,
                      pr: state.RequiredPR):
    """One required PR read on its own: (pr with its head and merge state,
    classification). None when unreadable: never evidence of a merge."""
    statuses = ()
    try:
        payload = deps.github.pr_view(target, pr.number, repo=pr.repo)
        args = dict(check_cursor=pr.check_cursor, conflict_cursor=pr.conflict_cursor)
        res = pr_poll.classify(payload, pr.feedback_cursor, login, **args)
        if (res.kind in ("quiet", "conflict") and not task.park
                and not task.feedback_pending):
            statuses = deps.github.ci_statuses(
                target, payload.get("headRefOid") or "",
                payload.get("headRefName") or "", repo=pr.repo)
            res = pr_poll.classify(payload, pr.feedback_cursor, login,
                                   ci_statuses=statuses, **args)
    except Exception as exc:     # an unreadable PR keeps its last known state
        print(f"[warn] PR poll failed for {pr.repo}#{pr.number}: {exc}", file=sys.stderr)
        return None
    # The check status outlives polls that skip CI (pending feedback, a park).
    pr = replace(pr, head=str(payload.get("headRefOid") or pr.head), outcome=res.kind,
                 checks=pr_poll.check_status(statuses, pr.checks),
                 state=res.kind if res.kind in ("merged", "closed") else "open")
    return pr, res


def _poll_required(cfg: Config, deps: Deps, target: Target, task: TaskState,
                   dry_run: bool = False) -> None:
    """A task with a required-PR set: every PR is read on its own cursors, the
    task finishes only when all merged, and a PR lost after another merged
    parks it for the operator (nothing is reverted or dropped)."""
    login = deps.github.viewer_login()
    # Closed PRs are read again: a reopened one must be able to recover.
    seen = {(pr.repo, pr.number): _observe_required(deps, target, task, login, pr)
            for pr in task.required_prs if pr.state != "merged"}
    unreadable = any(v is None for v in seen.values())
    revision = task.required_set_revision
    task = replace(task, required_prs=tuple(
        (seen.get((pr.repo, pr.number)) or (pr,))[0] for pr in task.required_prs))
    task = _reconcile_pending_repairs(task)
    save(cfg.state_dir, task)
    _settle_required(cfg, deps, target, task, [r for r in seen.values() if r], dry_run,
                     unreadable=unreadable, validated_revision=revision)


def _reconcile_pending_repairs(task: TaskState) -> TaskState:
    """Drop queued work for PRs that completed while execution was full."""
    if not task.feedback_pending:
        return task
    open_prs = {f"{p.repo}#{p.number}" for p in task.required_prs if p.state == "open"}
    repairs = tuple(key for key in task.repair_prs if key in open_prs)
    feedback = task.feedback_pr if task.feedback_pr in open_prs else ""
    return replace(task, repair_prs=repairs, feedback_pr=feedback,
                   feedback_pending=bool(repairs or feedback))


def _settle_required(cfg: Config, deps: Deps, target: Target, task: TaskState,
                     results: list, dry_run: bool, *, unreadable: bool = False,
                     validated_revision: int = 0) -> None:
    states = {p.state for p in task.required_prs}
    if states == {"merged"}:
        if _set_still_valid(cfg, task, validated_revision):
            _finish_required(cfg, deps, target, task, dry_run)
    elif "closed" in states:
        _required_pr_lost(cfg, deps, target, task, "merged" in states, unreadable)
    elif not task.park and not task.feedback_pending:
        _required_attention(cfg, deps, target, task, results)


def _set_still_valid(cfg: Config, task: TaskState, validated_revision: int) -> bool:
    """Completion and validation name one revision: the set must still be the
    one that was read, and cover every repository of the bundle."""
    current = load(cfg.state_dir, task.target, task.issue, repo=task.repo)
    return ((current is None or current.required_set_revision == validated_revision)
            and _required_set_covers_bundle(cfg, task))


def _required_set_covers_bundle(cfg: Config, task: TaskState) -> bool:
    bundle = read_bundle(cfg.state_dir, task.target, task.issue, repo=task.repo)
    return bundle is None or ({r.repo for r in bundle.repositories} - set(task.dropped_pr_repos)) <= {
        p.repo for p in task.required_prs}


def _partial_incident(task: TaskState) -> str:
    return f"{task.required_set_revision}|" + ";".join(sorted(
        f"{p.repo}#{p.number}:{p.state}" for p in task.required_prs))


def _finish_required(cfg: Config, deps: Deps, target: Target, task: TaskState,
                     dry_run: bool) -> None:
    last = task.required_prs[-1]
    try:
        _finish_merged(cfg, deps, target, task, dry_run,
                       url=f"https://github.com/{last.repo}/pull/{last.number}")
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"[warn] _finish_merged failed for #{task.issue}: {exc}", file=sys.stderr)


def _required_pr_lost(cfg: Config, deps: Deps, target: Target, task: TaskState,
                      partial: bool, unreadable: bool = False) -> None:
    """A required PR closed unmerged. After another merged, the task asks for
    guidance once per incident (whatever else parked it); otherwise it fails,
    but never while another PR could not be read: it may have merged."""
    if partial:
        if task.partial_incident != _partial_incident(task):
            _park_partial_merge(cfg, deps, target, task)
        elif task.partial_delivery == "sending":
            # Restart after a reserved send: delivery may have happened. The
            # durable console request remains the reply route; do not resend.
            save(cfg.state_dir, replace(task, partial_delivery="unknown"))
        return
    if unreadable:
        return
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    save(cfg.state_dir, replace(task, stage=Stage.FAILED, operator_request=None,
                                updated_at=_now()))
    lost = next(p for p in task.required_prs if p.state == "closed")
    eventlog.append_event(cfg.state_dir, "pr-closed", target=target.name,
                          issue=task.issue, repo=task.repo, stage=Stage.PR_OPEN.value,
                          detail=f"PR {lost.repo}#{lost.number} closed unmerged")
    _notify(deps, target, task, "pr_closed",
            f"PR {lost.repo}#{lost.number}. Worktree preserved for autopsy.")


def _park_partial_merge(cfg: Config, deps: Deps, target: Target, task: TaskState) -> None:
    note = "; ".join(f"{p.repo}#{p.number} {p.state}" for p in task.required_prs)
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    task = replace(task, park=PARK_HUMAN, park_msg_id=0, park_note=note,
                   park_reason=WHY_DECISION, slot=NO_SLOT, operator_request=None,
                   partial_incident=_partial_incident(task), partial_delivery="sending",
                   updated_at=_now())
    # Persist the request and stable incident identity before external delivery.
    # Telegram has no idempotency key: an ambiguous send remains visible in the
    # console rather than producing a second message/reply target after restart.
    save(cfg.state_dir, task)
    msg_id = deps.notifier.send(
        "partial_merge_guidance", issue=task.issue, repo=task.repo, title=task.title,
        url=_url(target, task.issue), target=target.name, note=note)
    save(cfg.state_dir, replace(task, park_msg_id=msg_id,
                                partial_delivery="delivered" if msg_id else "unknown"))
    eventlog.append_event(cfg.state_dir, "parked", target=target.name, issue=task.issue,
                          repo=task.repo, stage=task.stage.value,
                          detail="partial merge: " + note)


def _swap_required(task: TaskState, pr: state.RequiredPR, **kw) -> TaskState:
    return replace(task, required_prs=tuple(
        replace(p, **kw) if (p.repo, p.number) == (pr.repo, pr.number) else p
        for p in task.required_prs))


def _required_feedback(cfg: Config, deps: Deps, target: Target, task: TaskState,
                       pr: state.RequiredPR, res) -> None:
    """Review feedback on a required PR queues a round, up to the review cap per PR."""
    if pr.feedback_rounds >= cfg.loop_caps.review:
        _park_exhausted(cfg, deps, target, task,
                        f"review-feedback limit of {cfg.loop_caps.review} rounds "
                        f"reached for {pr.repo}#{pr.number}")
        return
    task = replace(_swap_required(loops.reset(task, ResetCause.PR_CYCLE_STARTED), pr,
                                  feedback_cursor=res.latest_ts,
                                  feedback_rounds=pr.feedback_rounds + 1),
                   feedback_pending=True, attention="feedback", repair_prs=(),
                   feedback_pr=f"{pr.repo}#{pr.number}", updated_at=_now())
    save(cfg.state_dir, task)
    eventlog.append_event(cfg.state_dir, "pr-feedback", target=target.name,
                          issue=task.issue, repo=task.repo,
                          stage=Stage.PR_OPEN.value, detail=f"{pr.repo}#{pr.number}")
    _notify(deps, target, task, "pr_feedback",
            f"PR {pr.repo}#{pr.number}, newest feedback {res.latest_ts}")


def _required_attention(cfg: Config, deps: Deps, target: Target, task: TaskState,
                        results: list) -> None:
    """Feedback or a red check on an open required PR queues one fix round."""
    for pr, res in results:
        if res.kind == "feedback":
            _required_feedback(cfg, deps, target, task, pr, res)
            return
    red = [(pr, res) for pr, res in results if res.kind in ("check-failed", "conflict")]
    if red:
        _required_ci_round(cfg, deps, target, task, red)


def _required_ci_round(cfg: Config, deps: Deps, target: Target, task: TaskState,
                       red: list) -> None:
    """Each red PR's cursor advances and it is queued for repair; the attempt
    is charged when the round launches (_charge_repairs). A PR already at the
    limit parks the task instead."""
    spent = [pr for pr, _ in red if pr.ci_fix_attempts >= CI_FIX_LIMIT]
    if spent:
        _park_exhausted(cfg, deps, target, task, f"CI-fix limit of {CI_FIX_LIMIT} attempts "
                        "reached for " + ", ".join(f"{p.repo}#{p.number}" for p in spent))
        return
    for pr, res in red:
        cursor = "check_cursor" if res.kind == "check-failed" else "conflict_cursor"
        task = _swap_required(task, pr, **{cursor: res.latest_ts})
    what = ", ".join(f"{res.kind} on {pr.repo}#{pr.number}" for pr, res in red)
    save(cfg.state_dir, replace(task, feedback_pending=True, attention=red[0][1].kind,
                                repair_prs=tuple(f"{pr.repo}#{pr.number}" for pr, _ in red),
                                feedback_pr="",
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "pr-attention", target=target.name,
                          issue=task.issue, repo=task.repo, stage=Stage.PR_OPEN.value,
                          detail=what)
    _notify(deps, target, task, "pr_attention", what)


def _pr_attention(cfg: Config, deps: Deps, target: Target, task: TaskState,
                  res: "pr_poll.PollResult") -> None:
    """A red check or a conflict on the open PR: one round of the ci loop,
    address-review queued with the reason, cursor advanced so the same
    condition never re-triggers."""
    cursor = ({"check-failed": dict(check_cursor=res.latest_ts),
               "conflict": dict(conflict_cursor=res.latest_ts)}[res.kind])
    task = replace(task, **cursor)          # advance cursor FIRST
    # At most CI_FIX_LIMIT automated fixes per PR, whatever the target configures.
    caps = replace(cfg.loop_caps, ci=min(cfg.loop_caps.ci, CI_FIX_LIMIT))
    if task.ci_fix_attempts >= CI_FIX_LIMIT:
        _park_exhausted(cfg, deps, target, task, f"CI-fix limit of {CI_FIX_LIMIT} "
                        f"attempts reached for PR #{task.pr_number}")
        return
    dec = loops.evaluate(task, loops.PRAttention(f"{res.kind} on PR #{task.pr_number}"),
                         caps)
    task, parked = _apply_loop_decision(
        cfg, deps, target, task, dec,
        park_exhausted=lambda t, note: _park_exhausted(cfg, deps, target, t, note))
    if parked:
        return          # cursor already applied; _park_exhausted saved the cursor-advanced task
    # not exhausted: set feedback_pending + attention, emit pr-attention event, pr_attention notify
    save(cfg.state_dir, replace(task, feedback_pending=True, attention=res.kind,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "pr-attention", target=target.name,
                          issue=task.issue, repo=task.repo, stage=Stage.PR_OPEN.value,
                          detail=res.kind)
    _notify(deps, target, task, "pr_attention", f"{res.kind} on PR #{task.pr_number}")


def _finish_merged(cfg: Config, deps: Deps, target: Target,
                   task: TaskState, dry_run: bool = False, url: str = "") -> None:
    """Ordered so a mid-sequence gh failure retries next pass (a merged PR
    still reads merged): board first (the raising step), then teardown
    (best-effort by construction), then the state flip that stops polling."""
    if target.status_done_option_id:
        deps.github.set_status(target, task.issue, target.status_done_option_id)
    else:
        print(f"[warn] {target.name}: status_done_option_id unset — board "
              f"not updated for #{task.issue}", file=sys.stderr)
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    if not dry_run:
        task_artifacts.collect(cfg.state_dir, task, target.repo)
        task_artifacts.pin_published(cfg.state_dir, task, target.repo)
    remove_workspace(target, task.worktree, task.branch, dry_run=dry_run)
    deps.github.delete_branch(target, task.branch)
    save(cfg.state_dir, replace(task, stage=Stage.DONE, park="",
                                feedback_pending=False, operator_request=None,
                                done_at=_now(), updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "merged", target=target.name,
                          issue=task.issue, repo=task.repo, stage=Stage.DONE.value,
                          detail=f"PR #{task.pr_number}")
    _notify(deps, target, task, "task_done",
            url or f"https://github.com/{target.repo}/pull/{task.pr_number}")


def _wake_blocked_path(cfg: Config, target: str, issue: int, repo: str = "") -> Path:
    return (Path(cfg.state_dir)
            / f"{WAKE_BLOCKED_PREFIX}{task_key(target, issue, repo)}")


def _mark_wake_blocked(cfg: Config, target: Target, task: TaskState,
                       reason: str) -> None:
    """Edge-triggered: the event fires the pass a wake FIRST goes hungry, not
    every pass thereafter — a resume that waits overnight must not write 144
    identical lines into events.jsonl. The marker is the edge."""
    p = _wake_blocked_path(cfg, task.target, task.issue, task.repo)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    eventlog.append_event(cfg.state_dir, "wake-blocked", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
                          detail=reason)


def _clear_wake_blocked(cfg: Config, target: str, issue: int, repo: str = "") -> None:
    _wake_blocked_path(cfg, target, issue, repo).unlink(missing_ok=True)


def _expired(task: TaskState, cutoff_seconds: int) -> bool:
    """Finished for at least the retention window. An unparseable or
    missing terminal_at never expires: fail closed, the card stays."""
    if task.stage not in TERMINAL_STAGES:
        return False
    try:
        since = datetime.fromisoformat(task.terminal_at)
    except (TypeError, ValueError):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - since).total_seconds() >= cutoff_seconds


def _project_member(cfg: Config, repo: str):
    return next((r for p in cfg.projects for r in p.repositories if r.repo == repo), None)


def _cleanup_target(cfg: Config, task: TaskState, by_name: dict):
    """What `remove_workspace` needs for `task`: its target or project member
    with the clone path of the task's own repository, else None (the worktree
    stays: nothing knows its clone)."""
    clone = _clone_map(cfg).get(task.repo) if task.repo else None
    target = by_name.get(task.target) or _project_member(cfg, task.repo)
    if target is None or (task.repo and not clone):
        return None
    return replace(target, clone_path=clone or target.clone_path)


def _flush_finished(cfg: Config, dry_run: bool = False, deps: Deps | None = None) -> None:
    """Retire finished tasks (done, failed, won't do) past the retention
    window: worktree and local branch go (the remote branch, PR and issue
    are the record), then the state file. A done task's teardown already
    ran at merge; remove_workspace is best-effort, so repeating it is a
    no-op. A task whose target left the config keeps its worktree: nothing
    knows its clone any more. A task whose sandbox resources could not be
    removed stays, so the cleanup is retried."""
    by_name = {t.name: t for t in cfg.targets}
    clone_of = _clone_of(cfg)
    for task in load_all(cfg.state_dir):
        if not _expired(task, cfg.done_retention_days * 86400):
            continue
        if (deps is not None and getattr(deps, "sandbox", None) is not None and not dry_run
                and not sandbox_lifecycle.cleanup(cfg, deps.sandbox, clone_of, task)):
            continue
        target = _cleanup_target(cfg, task, by_name)
        if target is not None and task.worktree:
            remove_workspace(target, task.worktree, task.branch, dry_run=dry_run)
        _clear_wake_blocked(cfg, task.target, task.issue, task.repo)
        clear_session(cfg.state_dir, task.target, task.issue, repo=task.repo)
        delete(cfg.state_dir, task.target, task.issue, repo=task.repo)
        eventlog.append_event(cfg.state_dir, "flushed",
                              target=task.target, issue=task.issue, repo=task.repo,
                              stage=task.stage.value)


def _clone_of(cfg: Config):
    clones = _clone_map(cfg)
    return lambda task: clones.get(task.repo or task.target)


def _settle_sandboxes(cfg: Config, deps: Deps, dry_run: bool) -> None:
    if getattr(deps, "sandbox", None) is not None and not dry_run:
        sandbox_lifecycle.settle(cfg, deps.sandbox, _clone_of(cfg))


def _wake_sandbox(cfg: Config, deps: Deps, target: Target, task: TaskState,
                  dry_run: bool = False) -> bool:
    """Start the task's services before its session runs again; when they will
    not start the task waits, marked, for the next pass. A dry run touches
    no real sandbox."""
    if dry_run or getattr(deps, "sandbox", None) is None or sandbox_lifecycle.wake(cfg, deps.sandbox, _clone_of(cfg), task):
        return True
    _mark_wake_blocked(cfg, target, task, "sandbox services did not start")
    return False


def _held(cfg: Config) -> int:
    return state.held_count(cfg.state_dir)


def _free_slot(cfg: Config) -> int | None:
    return allocate_slot(load_all(cfg.state_dir), max_slots(cfg.capacity),
                         reserved=state.reserved_slots(cfg.state_dir))


def _drop_finished_verifications(cfg: Config, verification) -> None:
    for held in state.allocations(cfg.state_dir):
        if held.owner_kind == "verification" and not verification.is_running(held.owner_id):
            state.release(cfg.state_dir, "verification", held.owner_id)


def _account_allocations(cfg: Config, deps: Deps, dry_run: bool,
                         sweep_running: bool) -> None:
    """Settle the non-task holders of the shared allocation view."""
    if dry_run:
        return
    if not sweep_running:
        state.release(cfg.state_dir, "triage", triage.TAB_LABEL)
    elif not any(a.owner_kind == "triage" for a in state.allocations(cfg.state_dir)):
        triage.hold_sweep(cfg)   # a restart lost the hold of a sweep still running
    _admit_verification(cfg, deps, sweep_running)


def _new_verification_slot(cfg: Config, sweep_running: bool) -> int | None:
    if claims.box_free(cfg.capacity, load_all(cfg.state_dir), sweep_running, _held(cfg)) <= 0:
        return None
    return _free_slot(cfg)


def _admit_verification(cfg: Config, deps: Deps, sweep_running: bool) -> None:
    """Verification holds an execution slot, never an unfinished-task place:
    a slot is kept while its process runs and dropped once it is gone."""
    verification = getattr(deps, "verification", None)
    if verification is None:
        return
    _drop_finished_verifications(cfg, verification)
    kept = {a.owner_id: a.slot for a in state.allocations(cfg.state_dir)
            if a.owner_kind == "verification"}
    for operation in verification.pending():
        # a held slot still guards live resources: retry their cleanup there
        slot = kept.get(operation, _new_verification_slot(cfg, sweep_running))
        if slot is None:
            continue   # no capacity for this one; a later operation may hold a slot to clean up
        state.hold(cfg.state_dir, "verification", operation, slot)   # durable before it starts
        try:
            verification.start(operation, slot)
        except Exception:
            # A launch that raised may still have a live process: keep the hold
            # until it is gone (_drop_finished_verifications releases it).
            if not verification.is_running(operation):
                state.release(cfg.state_dir, "verification", operation)
            log.exception("verification %s did not start", operation)


def _oldest_first(cfg: Config):
    """Sort key for box-wide queues: oldest `updated_at` first, ties in
    target-list order so a pass is deterministic."""
    rank = {t.name: i for i, t in enumerate(cfg.targets)}
    return lambda t: (t.updated_at, rank[t.target])


def _resume_woken(cfg: Config, deps: Deps, admit: Admit, order: Order,
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
        target = _task_target(cfg, targets[task.target], task)
        if task.ci_repair_pending and _repair_budget_exhausted(cfg, deps, target, task):
            continue
        held = _wake_held(cfg, deps, task)
        if held:
            _mark_wake_blocked(cfg, target, task, held)
            continue
        launch, why = _wake_launch(cfg, deps, target, task, admit, order)
        if why:
            _park_stranded(cfg, deps, target, task, why)
        if launch is None:
            continue
        if _box_free(cfg, load_all(cfg.state_dir)) <= 0:
            _mark_wake_blocked(cfg, target, task, "capacity full")
            continue
        task = _with_slot(cfg, target, task)
        if task is None or not _wake_sandbox(cfg, deps, target, task, dry_run):
            continue
        try:
            task = _reserve_ci_repair(cfg, deps, task)
            _resume_one(cfg, deps, target, task, launch)
            # The wake's launch is the next launch: a stored one-shot
            # override is used up with it, whatever model the wake ran on.
            _consume_execution_choice(cfg, task.target, task.issue, task.repo)
        except Exception:
            _fail_task_crash(cfg, deps, target, task, dry_run)


def _reserve_ci_repair(cfg: Config, deps: Deps, task: TaskState) -> TaskState:
    if not task.ci_repair_pending:
        return task
    # End the old execution before reserving: interruption recovery must
    # only adopt a session from this new repair attempt.
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    task = replace(_charge_repairs(task), ci_repair_pending=False,
                   repair_launch_reserved=True, park="")
    save(cfg.state_dir, task)
    return task


def _wake_held(cfg: Config, deps: Deps, task: TaskState) -> str:
    """Why a woken task may not launch yet, else ""."""
    # Only a launch record authorizes replacing a live execution. A
    # login park deliberately retains that execution until attach wakes it.
    if (_session_launch_blocked(cfg, deps, task)
            and not owns_task_session(cfg.state_dir, task.target, task.issue, task.repo)
            and not token_issued(cfg.state_dir, task.target, task.issue, task.repo)):
        return "session ownership is not isolated"
    if _awaits_spec_repo(cfg, task, Stage(next_stage(task))):
        return "awaiting the spec/plan repository choice"
    return ""


def _with_slot(cfg: Config, target: Target, task: TaskState) -> TaskState | None:
    """Parked tasks gave their slot back. Take any free one — worktrees are
    per-issue, and the spec and plan prompts name no slot port, so for a
    gate-parked task the number need not be the original. None: no slot is
    free; the task stays parked, retried next pass."""
    if task.slot != NO_SLOT:
        return task
    slot = _free_slot(cfg)
    if slot is None:
        _mark_wake_blocked(cfg, target, task, "no free slot")
        return None
    return replace(task, slot=slot)


def _wake_launch(cfg: Config, deps: Deps, target: Target, task: TaskState,
                 admit: Admit, order: Order) -> tuple[Launch | None, str]:
    """The launch a woken task resumes on: its next launch
    (state.next_launch), chosen as every launch is (_choose_launch). Its
    one-shot override is the model the resume named, else the stored
    execution override: "the next launch runs on this model" holds for a
    wake too. None: it waits (the gate denies the model; another task's may
    be admitted), with why it never will (_choose_launch) or ""."""
    stage = Stage(next_stage(task))
    override = execution_overrides.pending(
        _execution_override_for(cfg, task, stage),
        task.resume_model_override, task.resume_bypass_usage)
    launch, _, why = _choose_launch(cfg, target, task, stage, admit, order, override)
    return launch, why


ATTACH_TEXT = ("The operator is attaching to talk to you directly. "
               "Wait for their input.")


def _resume_one(cfg: Config, deps: Deps, target: Target,
                task: TaskState, launch: Launch) -> None:
    """Resume a woken task on `launch`. An attach wake tells the session to
    wait for the operator: the notice is queued, so it rides with the
    operator's messages in whatever the session reads first (the resume
    text or a fresh stage prompt), and it is taken back, and the operator
    is not pinged, when the launch fails."""
    # End first, unconditionally. Most parks already stopped the session,
    # but /attach on a PARK_LOGIN task reaches here with the pane still
    # LIVE, and _launch would then type the podman command INTO the
    # running session (the failure _retry_stage and SpawnStage guard).
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    if _session_launch_blocked(cfg, deps, task):
        raise RuntimeError("session ownership is not isolated")
    if not task.hold_for_attach:
        _launch_woken(cfg, deps, target, task, launch)
        return
    notice = messages.append(cfg.state_dir, task.target, task.issue,
                             ATTACH_TEXT, "dispatcher", repo=task.repo)
    try:
        detail = _launch_woken(cfg, deps, target, task, launch)
    except Exception:
        messages.mark_delivered(cfg.state_dir, task.target, task.issue,
                                [notice.id], repo=task.repo)
        raise
    deps.notifier.send("resumed_for_attach", issue=task.issue, repo=task.repo,
                       title=task.title, url=_url(target, task.issue),
                       target=target.name, note="new conversation" if detail else "")


def _launch_woken(cfg: Config, deps: Deps, target: Target,
                  task: TaskState, launch: Launch) -> str:
    """Launch a woken task. Returns why a new conversation replaced the
    recorded one, else ""."""
    entry = launch.entry
    _heal_exclude(target, task)
    # The two fresh-spawn paths below write stage.json and the models.log
    # line themselves (_spawn_stage), with the stage that is launched.
    if task.crashed_stage or (task.stage is Stage.IMPLEMENT
                              and implement_done(task)):
        # No session to continue: the stage it died in, or review after a
        # done implement session, starts afresh (state.next_launch).
        _respawn(cfg, deps, target, task, launch)
        return ""
    if task.stage is Stage.PR_OPEN:
        # No session to continue at pr-open: an operator wake on a parked
        # pr-open task is a fresh address-review round carrying their message.
        task = loops.reset(task, ResetCause.PR_CYCLE_STARTED)
        task = replace(task, park="", park_msg_id=0, park_note="",
                       hold_for_attach=False, feedback_pending=False,
                       attention="operator", feedback_cursor=_cursor_now(),
                       resume_model_override="", resume_bypass_usage=False,
                       operator_request=None)
        _clear_wake_blocked(cfg, task.target, task.issue, task.repo)
        _spawn_stage(cfg, deps, target, task, launch)
        eventlog.append_event(cfg.state_dir, "resumed", target=target.name,
                              issue=task.issue, repo=task.repo, stage=Stage.ADDRESS_REVIEW.value,
                              model=str(entry))
        return ""
    # Before resuming: the next pass must not re-read blocked/awaiting-ci
    # and re-park the freshly resumed session.
    _write_working(task.worktree, task.stage, entry)
    _log_model(task.worktree, task.stage, str(entry))
    task, detail = _continue_or_restart(cfg, deps, target, task, launch)
    _clear_wake_blocked(cfg, task.target, task.issue, task.repo)
    save(cfg.state_dir, replace(task, park="", hold_for_attach=False,
                                repair_launch_reserved=False,
                                park_msg_id=0, park_note="",
                                resume_model_override="",
                                resume_bypass_usage=False,
                                operator_request=None,
                                picks={**task.picks,
                                      pick_key(task.stage.value): str(entry)},
                                updated_at=_now()))
    return detail


def _crash_failed(task: TaskState) -> TaskState:
    """FAILED by a crash, remembering the stage of its next launch
    (state.next_launch) so Resume can respawn it: its own stage; for a stage
    with no session of its own (the PR is open, the spec waits at its gate)
    the stage that has one; for a task that is already a resumable crash,
    whose turn failed again, the stage it recorded."""
    stage = next_stage(task)
    crashed = stage if Stage(stage) in RESPAWNABLE_STAGES else ""
    return replace(task, stage=Stage.FAILED, crashed_stage=crashed, park="",
                   hold_for_attach=False, operator_request=None,
                   updated_at=_now())


def _respawn(cfg: Config, deps: Deps, target: Target,
             task: TaskState, launch: Launch) -> None:
    """Resume of a task that has no session to continue; `launch` is its
    next launch. A crashed task: the stage it died in starts afresh in the
    same worktree; an implement session continues from its ledger there. A
    fresh stage prompt, not a resume: the crashed launch may never have
    started a conversation. The queued messages ride in the stage prompt,
    an attach notice among them."""
    detail = "after crash" if task.crashed_stage else "next launch"
    deps.github.set_status(target, task.issue,
                           target.status_in_progress_option_id)
    task = replace(task, crashed_stage="", park="", park_msg_id=0,
                   park_note="", hold_for_attach=False,
                   resume_model_override="", resume_bypass_usage=False,
                   **loops.crash_resume_fields(task))
    _clear_wake_blocked(cfg, task.target, task.issue, task.repo)
    task = _spawn_stage(cfg, deps, target, task, launch)
    eventlog.append_event(cfg.state_dir, "resumed", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
                          model=str(launch.entry), detail=detail)


def _fail_task_crash(cfg: Config, deps: Deps, target: Target,
                     task: TaskState, dry_run: bool = False) -> None:
    """One task's turn blew up. Fail THAT task; let the pass carry on.

    Everything a task's turn touches is reachable through its worktree, and
    a worktree can simply be gone — swept, half-removed, or deleted by hand.
    The launch then raises writing into the missing worktree, and before
    this the exception unwound all the way out of run_pass: guarded_pass
    filed a pass-crash and re-raised, so the unit failed and every task
    QUEUED BEHIND the broken one was never reached. One dead checkout stopped
    the whole box, every firing, until a human looked. A card in Failed is
    the far cheaper outcome, and the report carries the traceback.

    The failure is written from the saved state: the turn may have persisted
    a step (a done ticket, a started one) before it failed, and the caller's
    `task` predates that write. From the caller's task when the state file
    cannot be read: this function must not raise."""
    error = traceback.format_exc()
    try:
        task = load(cfg.state_dir, task.target, task.issue) or task
    except Exception as exc:
        print(f"[warn] state of #{task.issue} unreadable after its crash: "
              f"{exc}", file=sys.stderr)
    # Best-effort teardown: the point of this function is that nothing in it
    # may raise, or we are back to killing the pass.
    try:
        deps.github.release(target, task.issue, "task crashed mid-pass")
    except Exception as exc:
        print(f"[warn] release failed for #{task.issue}: {exc}", file=sys.stderr)
    try:
        log_tail = deps.sessions.capture_tail(task.target, task.issue, **_repo_kw(task), lines=30)
    except Exception:
        log_tail = ""
    save(cfg.state_dir, _crash_failed(task))
    eventlog.append_event(cfg.state_dir, "failed", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
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


def _awaiting_feedback_round(task: TaskState) -> bool:
    """A pr-open task with pending feedback that _spawn_feedback launches.
    A park keeps the feedback and leaves it to the wake that starts the
    round (_launch_woken); a park lifted without one comes back here."""
    return task.stage is Stage.PR_OPEN and task.feedback_pending and not task.park


def _spawn_feedback(cfg: Config, deps: Deps, admit: Admit,
                    order: Order, dry_run: bool = False) -> None:
    """Spawn address-review for tasks whose PR got feedback, box-wide and
    oldest first — same gates as claiming new work (capacity, usage, slot);
    a denied spawn just stays pr-open+pending and retries next pass, badge
    showing, unless it never launches (_choose_launch): then it parks with
    its feedback kept (_awaiting_feedback_round)."""
    targets = {t.name: t for t in cfg.targets}
    pending = sorted(
        [t for t in load_all(cfg.state_dir)
         if t.target in targets and _awaiting_feedback_round(t)],
        key=_oldest_first(cfg))
    for task in pending:
        target = _task_target(cfg, targets[task.target], task)
        launch, bypass_usage, why = _choose_launch(
            cfg, target, task, Stage.ADDRESS_REVIEW, admit, order,
            _execution_override_for(cfg, task, Stage.ADDRESS_REVIEW))
        if why:
            _park_stranded(cfg, deps, target, task, why)
        if launch is None:
            continue
        if _box_free(cfg, load_all(cfg.state_dir)) <= 0:
            _mark_wake_blocked(cfg, target, task, "capacity full")
            continue
        slot = _free_slot(cfg)
        if slot is None:
            _mark_wake_blocked(cfg, target, task, "no free slot")
            continue
        if not _wake_sandbox(cfg, deps, target, task, dry_run):
            continue
        _clear_wake_blocked(cfg, task.target, task.issue, task.repo)
        # Cursor := spawn time: everything the session can read live is
        # now "seen"; anything arriving after this moment re-triggers a
        # round (conservative — a redundant round beats a lost comment).
        # _cursor_now(), not _now(): GitHub timestamps are second-granular
        # and pr_poll's trigger is a strict `>`, so the cursor has to be
        # quantised to seconds or a comment from the spawn second is lost.
        # Retire the implement session before recording the repair reservation:
        # recovery must never mistake that older session for a launched repair.
        _end_session(cfg, deps, task.target, task.issue, task.repo)
        task = replace(_charge_repairs(task), slot=slot, feedback_pending=False,
                       feedback_cursor=_cursor_now(), stage=Stage.ADDRESS_REVIEW,
                       picks={**task.picks, pick_key(Stage.ADDRESS_REVIEW.value): str(launch.entry)},
                       repair_launch_reserved=True)
        save(cfg.state_dir, task)
        _spawn_stage(cfg, deps, target, task, launch)
        _consume_execution_choice(cfg, task.target, task.issue, task.repo)


def _charge_repairs(task: TaskState) -> TaskState:
    """The fix round is launching: each PR it repairs spends one CI-fix attempt."""
    if not task.required_prs and task.attention in ("check-failed", "conflict"):
        return replace(task, ci_fix_attempts=task.ci_fix_attempts + 1)
    for key in task.repair_prs:
        repo, _, number = key.partition("#")
        pr = next((p for p in task.required_prs if (p.repo, str(p.number)) == (repo, number)), None)
        if pr:
            task = _swap_required(task, pr, ci_fix_attempts=pr.ci_fix_attempts + 1)
    return task


def _repair_budget_exhausted(cfg: Config, deps: Deps, target: Target,
                             task: TaskState) -> bool:
    """Automatic verification retries share the lifetime PR repair allowance."""
    spent = [f"{p.repo}#{p.number}" for p in task.required_prs
             if f"{p.repo}#{p.number}" in task.repair_prs
             and p.ci_fix_attempts >= CI_FIX_LIMIT]
    if not task.required_prs and task.ci_fix_attempts >= CI_FIX_LIMIT:
        spent = [f"{task.repo}#{task.pr_number}"]
    if not spent:
        return False
    _park_exhausted(cfg, deps, target, replace(task, ci_repair_pending=False),
                    f"CI-fix limit of {CI_FIX_LIMIT} attempts reached for " + ", ".join(spent))
    return True


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
    launch_failed: bool = False   # a launch raised: the operator's choice is not used up


def _action_stage(act: object) -> Stage | None:
    """The stage an action launches a session for; None when it launches none."""
    if isinstance(act, RetryStage):
        return act.stage  # _retry_stage resumes the session in place
    return act.stage if isinstance(act, SpawnStage) else None


def _step_done(turn: _Turn, task: TaskState, act: object) -> TaskState:
    """The task with the step `act` says is complete persisted, in a write of
    its own, BEFORE the next launch is chosen and admitted: every reader of
    the state (state.next_launch) then names the launch that waits. The
    implement session done: its pick is dropped as review is chosen, so the
    first PR feedback session chooses from its own list (models.FEEDBACK_PICK)
    and never inherits the implement provider."""
    if (task.stage is not Stage.IMPLEMENT or _action_stage(act) is not Stage.REVIEW
            or IMPLEMENT_PICK not in task.picks):
        return task
    done = replace(task, picks={k: v for k, v in task.picks.items()
                                if k != IMPLEMENT_PICK})
    save(turn.cfg.state_dir, done)
    return done


# Handlers return the task to keep driving, or None to end the task's turn.

def _on_noop(turn: _Turn, task: TaskState, act: NoOp,
             launch: Launch | None) -> TaskState:
    return task


def _on_apply_decision(turn: _Turn, task: TaskState, act: ApplyDecision,
                       launch: Launch | None) -> TaskState | None:
    cfg, deps, target = turn.cfg, turn.deps, turn.target
    task, parked = _apply_loop_decision(
        cfg, deps, target, task, act.decision,
        park_exhausted=lambda t, note: _park_for_input(cfg, deps, target, t, note,
                                                       reason=WHY_LOOP_CAP))
    return None if parked else task


def _on_park_for_input(turn: _Turn, task: TaskState, act: ParkForInput,
                       launch: Launch | None) -> None:
    _park_for_input(turn.cfg, turn.deps, turn.target, task, act.note,
                    artifact=act.artifact, is_answers=act.is_answers,
                    reason=act.reason)


def _plan_approval(task: TaskState, artifact: str) -> PlanApprovalRequest:
    """Arm the gate's request: the plan session's review page, bound to the
    plan revision that is on disk now. The answers of an earlier round go
    (answers.discard_stale)."""
    revision = task_plan_revision(task, artifact)
    answers.discard_stale(task.worktree, "plan-approval", revision.fingerprint)
    return PlanApprovalRequest(path=revision.path, fingerprint=revision.fingerprint)


def _on_disarm_plan_approval(turn: _Turn, task: TaskState,
                             act: DisarmPlanApproval,
                             launch: Launch | None) -> TaskState:
    task = replace(task, operator_request=None)
    save(turn.cfg.state_dir, task)
    return task


def _on_park_for_review(turn: _Turn, task: TaskState, act: ParkForReview,
                        launch: Launch | None) -> None:
    if act.artifact is not None:
        task = replace(task, operator_request=_plan_approval(task, act.artifact))
    _park_for_review(turn.cfg, turn.deps, turn.target, task, act.reason,
                     dry_run=turn.dry_run)


def _on_park_for_ci(turn: _Turn, task: TaskState, act: ParkForCI,
                    launch: Launch | None) -> None:
    _park_for_ci(turn.cfg, turn.deps, turn.target, task, act.run_id)


def _on_retry_stage(turn: _Turn, task: TaskState, act: RetryStage,
                    launch: Launch) -> None:
    _retry_stage(turn.cfg, turn.deps, turn.target, task, launch, act)


def _stage_extra(task: TaskState, act: SetTaskStage, signal) -> dict:
    """The fields a stage transition sets besides the stage itself: the PR
    number a pr-open signal links, or the plan-approval request a finished
    plan arms. Any other transition drops a request: nothing is left to
    approve on a task that failed at the gate."""
    if act.stage is Stage.AWAITING_PLAN_REVIEW:
        extra: dict = {"operator_request": _plan_approval(task, act.artifact),
                       **loops.gate_fields(task)}
        if task.stage is Stage.PLAN:
            # The gate phase has retries of its own.
            extra.update(plan_retries=0, plan_slips=0)
        return extra
    extra = {"operator_request": None}
    if act.stage is Stage.PR_OPEN and signal is not None:
        m = re.search(r"/pull/(\d+)", signal.artifact or signal.note or "")
        if m:
            extra["pr_number"] = int(m.group(1))
    return extra


def _on_set_task_stage(turn: _Turn, task: TaskState, act: SetTaskStage,
                       launch: Launch | None) -> TaskState:
    cfg = turn.cfg
    clear_turn_markers(cfg.state_dir, task.target, task.issue, repo=task.repo)
    task = replace(task, stage=act.stage, updated_at=_now(),
                   **_stage_extra(task, act, turn.signal))
    save(cfg.state_dir, task)
    if act.stage is Stage.PR_OPEN:
        eventlog.append_event(cfg.state_dir, "pr-opened",
                              target=turn.target.name, issue=task.issue, repo=task.repo,
                              stage=act.stage.value)
    return task


def _on_publish_spec(turn: _Turn, task: TaskState, act: PublishSpec,
                     launch: Launch | None) -> TaskState:
    pub = _publish_spec(turn.cfg, task, turn.target, turn.dry_run)
    if pub is None:   # publication waits for the spec repository selection
        return task
    turn.spec_line = _spec_note(pub)
    if act.review and not pub.error:
        try:
            turn.deps.github.comment(
                turn.target, task.issue, f"📝 Plan ready for review: {pub.url}")
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
                    launch: Launch) -> TaskState | None:
    # The previous stage's session is usually still alive here — an
    # interactive session cannot exit itself. _launch would type
    # the next stage's podman command INTO it (and the container
    # name would collide). End it first; no-op when already dead.
    _end_session(turn.cfg, turn.deps, task.target, task.issue, task.repo)
    # The spec session's `done` hands spec.md and the track over to plan; a
    # plan session respawned from the gate keeps both.
    spec_path = ""
    if task.stage is Stage.SPEC and act.stage is Stage.PLAN:
        spec_path = turn.signal.artifact
        task = replace(task, track=turn.signal.track)
    elif task.stage is Stage.AWAITING_PLAN_REVIEW:
        task = replace(task, **loops.gate_fields(task, respawn=act.stage is Stage.PLAN))
    if act.tickets:
        task = replace(task, ticket_count=act.tickets)
    if not _exploration_handover(turn, task, act.stage):
        return None
    try:
        return _spawn_stage(turn.cfg, turn.deps, turn.target, task, launch, spec_path)
    except Exception:
        # What the previous stage handed over is accepted (a checked spec, an
        # approved or gate-free plan); only this launch failed. So the task
        # fails as crashed in the stage it could not start, and Resume starts
        # that stage: an implement task asks for no second approval, and no
        # earlier stage runs again. The hand-over is saved first: the failure
        # is written from the saved state (_fail_task_crash).
        turn.launch_failed = True
        accepted = replace(task, stage=act.stage,
                           spec_path=spec_path or task.spec_path)
        try:
            save(turn.cfg.state_dir, accepted)
        except Exception as exc:
            print(f"[warn] hand-over of #{task.issue} not saved: {exc}",
                  file=sys.stderr)
        _fail_task_crash(turn.cfg, turn.deps, turn.target, accepted,
                         turn.dry_run)
        return None


def _on_handle_crash(turn: _Turn, task: TaskState, act: HandleCrash,
                     launch: Launch | None) -> TaskState:
    cfg, deps, target = turn.cfg, turn.deps, turn.target
    _notify(deps, target, task, "session_crashed")
    deps.github.release(target, task.issue, "session crashed mid-stage")
    save(cfg.state_dir, _crash_failed(task))
    eventlog.append_event(cfg.state_dir, "failed", target=target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
                          detail="session crashed mid-stage")
    _report_session_crash(cfg, deps, target, task, turn.dry_run)
    # End last: _report_session_crash reads the pane first. end()
    # snapshots the crash output for the console and closes the
    # dead tab — the one session-ending transition that otherwise
    # left both behind.
    _end_session(cfg, deps, task.target, task.issue, task.repo)
    return task


def _on_record_background_wait(turn: _Turn, task: TaskState,
                               act: RecordBackgroundWait,
                               launch: Launch | None) -> TaskState:
    task = replace(task, background_reported=act.reported,
                   background_seq=act.seq, **NO_POKES)
    save(turn.cfg.state_dir, task)
    return task


STOPPED_NOTE = "(session stopped mid-stage waiting for input)"


class _PokeGuard(Enum):
    """What herdr and the pane say right before a poke is typed."""
    UNKNOWN = "herdr or the pane cannot be read"
    WORKING = "the session started a turn after the idle read"
    AT_PROMPT = "a permission, trust or login prompt"
    CLEAR = "a stopped session at its composer"


def _poke_guard(deps: Deps, task: TaskState) -> _PokeGuard:
    """One read of herdr and one of the pane. Text plus Enter in a prompt
    can approve an action without the operator, so a read that failed is
    never taken as "no prompt"."""
    state = deps.sessions.agent_state(task.target, task.issue, **_repo_kw(task))
    tail = deps.sessions.capture_tail(task.target, task.issue, **_repo_kw(task))
    if state is None or not tail.strip():
        return _PokeGuard.UNKNOWN
    if state[0] == "working":
        return _PokeGuard.WORKING
    if state[0] == "blocked" or relogin.classify_login(tail) is not None:
        return _PokeGuard.AT_PROMPT
    return _PokeGuard.CLEAR


def _send_poke(turn: _Turn, task: TaskState, act: Poke) -> TaskState:
    """The first attempt starts the cap clock, delivered or not: a pane that
    refuses text every pass is then still parked at the cap. Only a
    delivered poke is counted and logged."""
    task = replace(task, poked_since=task.poked_since or time.time())
    if not turn.deps.sessions.send_text(task.target, task.issue, act.text, **_repo_kw(task)):
        save(turn.cfg.state_dir, task)
        print(f"[warn] poke of #{task.issue} not delivered", file=sys.stderr)
        return task
    task = replace(task, pokes=task.pokes + 1)
    save(turn.cfg.state_dir, task)
    eventlog.append_event(turn.cfg.state_dir, "poked", target=turn.target.name,
                          issue=task.issue, repo=task.repo, stage=task.stage.value,
                          detail=f"poke {task.pokes}")
    return task


def _on_poke(turn: _Turn, task: TaskState, act: Poke,
             launch: Launch | None) -> TaskState | None:
    """Type the poke into the stopped session, unless the look at herdr and
    the pane right before it says otherwise: unknown holds (nothing typed,
    nothing changed, the next pass tries again), a new turn ends the poke
    stretch, and a prompt parks the task with nothing typed."""
    if turn.dry_run:
        return task
    guard = _poke_guard(turn.deps, task)
    if guard is _PokeGuard.UNKNOWN:
        return task
    if guard is _PokeGuard.WORKING:
        return _end_poke_stretch(turn.cfg, task, 0.0, False)
    if guard is _PokeGuard.AT_PROMPT:
        return _on_park_for_input(turn, task, ParkForInput(STOPPED_NOTE), launch)
    return _send_poke(turn, task, act)


_DRIVE: dict[type, Callable[..., TaskState | None]] = {
    NoOp: _on_noop,
    RecordBackgroundWait: _on_record_background_wait,
    Poke: _on_poke,
    ApplyDecision: _on_apply_decision,
    ParkForInput: _on_park_for_input,
    DisarmPlanApproval: _on_disarm_plan_approval,
    ParkForReview: _on_park_for_review,
    ParkForCI: _on_park_for_ci,
    RetryStage: _on_retry_stage,
    SetTaskStage: _on_set_task_stage,
    PublishSpec: _on_publish_spec,
    Notify: _on_notify,
    SpawnStage: _on_spawn_stage,
    HandleCrash: _on_handle_crash,
}


def _backfill_track(cfg: Config, policy: ModelPolicy, task: TaskState,
                    signal: StageSignal | None,
                    dry_run: bool) -> tuple[TaskState, StageSignal | None]:
    """Claimed before tracks existed (#121): no triage label was read and
    its spec signal names none. Run it as untracked work, the same as a
    candidate without a track label, and record that for the console."""
    task = replace(task, track=policy.untracked)
    if not dry_run:
        save(cfg.state_dir, task)
    if signal is not None and not signal.track:
        signal = replace(signal, track=task.track)
    return task, signal


def _adopt_track(cfg: Config, policy: ModelPolicy, task: TaskState,
                 signal: StageSignal | None,
                 dry_run: bool) -> tuple[TaskState, StageSignal | None]:
    """The task's track for this turn, settled before anything reads it."""
    if not task.track:
        task, signal = _backfill_track(cfg, policy, task, signal, dry_run)
    # Two signals set the track for every later stage (see StageSignal.track):
    # the spec session's `done` and the approval, a `done` at the plan gate.
    # Adopt it before anything below reads task.track —
    # the "track configured" guard and the launch this same turn may spawn
    # (e.g. PLAN off a spec "done" signal) both need the fresh value, not
    # whatever was recorded when the task was last saved. Only a CONFIGURED
    # track is adopted here: an unknown/misspelled one must reach
    # next_actions' bounce-then-park ladder (_bad_track) instead of being
    # written onto the task and mis-parked as "no longer configured".
    if (signal is not None and signal.status == "done"
            and task.stage in (Stage.SPEC, Stage.AWAITING_PLAN_REVIEW)
            and signal.track and signal.track != task.track
            and signal.track in policy.tracks):
        task = replace(task, track=signal.track)
    return task, signal


def _background_view(cfg: Config, deps: Deps, task: TaskState,
                     alive: bool) -> BackgroundView | None:
    """The task's background wait as this pass sees it; herdr is asked only
    when a live session has a background marker."""
    wait = read_background(cfg.state_dir, task.target, task.issue,
                           repo=task.repo) if alive else None
    if wait is None:
        return None
    return BackgroundView(wait, deps.sessions.agent_state(task.target, task.issue, **_repo_kw(task)),
                          time.time(), cfg.background_wait_seconds)


def _end_poke_stretch(cfg: Config, task: TaskState, idle: float | None,
                      dry_run: bool) -> TaskState:
    """herdr sees the session working (idle 0): whatever woke it, the poke
    stretch is over and the next poke starts a new clock."""
    if idle != 0 or not task.poked_since or dry_run:
        return task
    task = replace(task, **NO_POKES)
    save(cfg.state_dir, task)
    return task


def _park_stranded(cfg: Config, deps: Deps, target: Target, task: TaskState,
                   why: str) -> None:
    """Park a task the config strands for `why` (models.stranded)."""
    _park_for_input(cfg, deps, target, task,
                    f"{why}; restore it in the console's model settings or "
                    f"targets.yaml, or run the task with a model override",
                    reason=WHY_CONFIG)


def _reconcile_repair_launch(cfg: Config, deps: Deps, target: Target,
                             task: TaskState) -> None:
    if deps.sessions.is_alive(task.target, task.issue, **_repo_kw(task)):
        save(cfg.state_dir, replace(task, repair_launch_reserved=False))
        return
    # A spawn may have succeeded before the process died. Never start another
    # automated attempt from this reservation. The operator can guide recovery.
    _park_exhausted(cfg, deps, target, replace(task, repair_launch_reserved=False),
                    "Repair launch interrupted; attempt reserved. Confirm recovery before resuming.")


def _drive_task(cfg: Config, deps: Deps, target: Target, task: TaskState,
                admit: Admit, order: Order, dry_run: bool = False) -> None:
    if task.repair_launch_reserved:
        _reconcile_repair_launch(cfg, deps, target, task)
        return
    signal = read_stage_signal(task.worktree)
    policy = policy_for(cfg, target)
    task, signal = _adopt_track(cfg, policy, task, signal, dry_run)
    alive = deps.sessions.is_alive(task.target, task.issue, **_repo_kw(task))
    waiting = has_waiting(cfg.state_dir, task.target, task.issue, repo=task.repo)
    why = track_gone(policy, task.track)
    if why:
        _park_stranded(cfg, deps, target, task, why)
        return
    # herdr's view of a background wait BEFORE the idle reading: a session
    # waking between the two reads must look still-waiting (held), never
    # woken-with-a-stale-idle (stall-parked mid-turn).
    view = _background_view(cfg, deps, task, alive)
    # Query idle only when it can matter: detection enabled and the
    # session alive (the crash path owns dead sessions).
    stall_after = cfg.stall_after_seconds if alive else 0
    idle = (deps.sessions.idle_seconds(task.target, task.issue, **_repo_kw(task))
            if alive and stall_after > 0 else None)
    task = _end_poke_stretch(cfg, task, idle, dry_run)
    turn = _Turn(cfg, deps, target, signal, dry_run)
    for act in pass_actions(task, signal, alive, waiting, view,
                            caps=cfg.loop_caps,
                            idle_seconds=idle,
                            stall_after=stall_after,
                            grace_elapsed=_grace_elapsed(cfg, task),
                            tracks=frozenset(policy.tracks),
                            gate_free=frozenset(
                                n for n, t in policy.tracks.items()
                                if not t.plan_review),
                            now=time.time(), poke_cap=cfg.background_wait_seconds):
        task = _step_done(turn, task, act)
        stage = _action_stage(act)
        launch, bypass_usage, why = _action_launch(cfg, target, task, stage, admit, order)
        if why:
            _park_stranded(cfg, deps, target, task, why)
        if stage is not None and launch is None:
            return  # no launch: the signal persists; retried once headroom returns
        choice_key = (task.target, task.issue, task.repo)
        task = _DRIVE[type(act)](turn, task, act, launch)
        if launch is not None and not turn.launch_failed:
            _consume_execution_choice(cfg, *choice_key)
        if task is None:
            return


def _report_session_crash(cfg: Config, deps: Deps, target: Target,
                          task: TaskState, dry_run: bool) -> None:
    # The stage's pick names its runtime. With no pick the session that
    # died is the worktree's last launch: a ticket is done and the next one
    # waits, so the implement pick is gone, and the done ticket's session,
    # still open, signalled `working` again (an operator attached to it)
    # before it died. A pre-picks task ran on Claude, which is what a bare
    # (here: empty) id resolves to.
    pick = (stage_pick(task.picks, task.stage.value)
            or _last_launched(task.worktree))
    runtime = runtime_for(parse_entry(pick, "pick").model_id if pick else "")
    record = read_session(cfg.state_dir, task.target, task.issue)
    repro = "Restart the stage from the dispatcher: no session recorded"
    if record is not None and record.stage == task.continued_stage.value:
        repro = (f"cd {shlex.quote(task.worktree)} && "
                 f"{runtime.resume_cmd(record.session_id)}  # inside session image")
    rep = failures.FailureReport(
        klass="session-crash", target=target.name, issue=task.issue,
        title=f"session crashed during {task.stage.value}: {task.title}",
        error=(f"session task-{task.target}-{task.issue} died during stage "
               f"{task.stage.value}"),
        log_tail=deps.sessions.capture_tail(task.target, task.issue, **_repo_kw(task), lines=30),
        repro=repro,
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
        worktree=wt, repo=cand.repo)
    blocker = failures.report_failure(cfg, deps, rep, dry_run=dry_run)
    # Quarantine only once the report exists (marker written) — a gh outage
    # leaves neither, so the next pass retries both. blocker may still be 0
    # when infra_repo is unset; that record blocks until manually deleted.
    if not dry_run and failures.reported(cfg.state_dir, rep):
        failures.write_quarantine(cfg.state_dir, target.name, cand.number,
                                  blocker_repo=cfg.infra_repo if blocker else "",
                                  blocker_issue=blocker,
                                  fp=failures.fingerprint(rep),
                                  repo=cand.repo)


def _box_free(cfg: Config, tasks: list[TaskState]) -> int:
    """claims.box_free for this pass. `cfg` is the pass's effective config,
    whose capacity already has a running triage sweep's unit taken off."""
    return claims.box_free(cfg.capacity, tasks, triage_running=False, held=_held(cfg))


def _reopened(stale: TaskState, pass_started: str) -> bool:
    """Reopened won't-do: rank drops CLOSED issues, so a candidate row for a
    canceled task proves the operator reopened the issue and moved the card
    back to Ready; its tombstone is swept for a fresh claim. The pass_started
    guard keeps a tombstone written by THIS pass's cancel intent (its board
    write lagging or failed) from resurrecting the task it just retired."""
    return (stale.stage is Stage.CANCELED
            and not (pass_started and stale.updated_at >= pass_started))


def _claimable(cfg: Config, deps: Deps, target: Target, tasks: list[TaskState],
               admit: Admit, order: Order, pass_started: str):
    """Ranked candidates this pass may claim, each with the spec launch it
    would start. Skips issues that already have a task, quarantined issues,
    and candidates whose spec model has no headroom — a denied candidate
    leaves the next one eligible, since its model may differ."""
    by_issue = {(t.repo, t.issue): t for t in tasks if t.target == target.name}
    # A project ranks across all its repositories, not the first one's.
    source = next((p for p in cfg.projects if p.key == target.name), target)
    for cand in deps.github.candidates(source):
        key = (_task_repo(cfg, target, cand), cand.number)
        # A legacy record (repo "") still speaks for its issue number.
        stale = by_issue.get(key) or by_issue.get(("", cand.number))
        if stale is not None and not _reopened(stale, pass_started):
            continue
        if failures.check_quarantine(cfg.state_dir, deps.github, target.name,
                                     cand.number, repo=cand.repo):
            continue
        launch, _bypass = _candidate_launch(
            cfg, _exec_target(cfg, target, cand), cand, admit, order)
        if launch is not None:
            yield cand, launch


def _exec_target(cfg: Config, target: Target, cand: Candidate) -> Target:
    """The target a candidate runs under: for a project, its own repository."""
    p = next((p for p in cfg.projects if p.key == target.name), None)
    return project_target(p, cand.repo) if p else target


def _task_target(cfg: Config, target: Target, task: TaskState) -> Target:
    """The target an existing task runs under: for a project, the task's own
    repository (commands, PR lookups and model policy), not the first one's."""
    p = next((p for p in cfg.projects if p.key == target.name), None)
    return project_target(p, task.repo) if p and task.repo else target


def _task_repo(cfg: Config, target: Target, cand: Candidate) -> str:
    return cand.repo or (target.repo if any(
        p.key == target.name for p in cfg.projects) else "")


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
                     repo=_task_repo(cfg, target, cand),
                     effort=cand.effort, labels=cand.labels,
                     track=track_from_labels(cand.labels, policy_for(cfg, target)))
    save(cfg.state_dir, task)  # state exists BEFORE the irreversible claim, so a partial claim is recoverable
    eventlog.append_event(cfg.state_dir, "claimed", target=target.name,
                          issue=cand.number, stage=Stage.QUEUED.value)
    try:
        deps.github.claim(target, cand)  # irreversible board mutation — last
        if _held_for_spec_repo(cfg, deps, target, task, launch.stage):
            return task
        task = _spawn_stage(cfg, deps, target, task, launch)
        _consume_execution_choice(cfg, target.name, cand.number, cand.repo)
    except Exception:
        deps.github.release(target, cand.number, "claim/spawn failed after provisioning")
        raise
    return task


def _claim_denied(cfg: Config, name: str, tasks: list[TaskState],
                  enabled: list[str]) -> bool:
    """True when the total limit or another project's reservation leaves
    `name` no place among `tasks`."""
    return bool(claims.claim_room(cfg.capacity, cfg.max_open, tasks, False, _held(cfg)) <= 0
                or claims.reservation_denial(name, tasks, enabled, cfg.max_open,
                                     claims.held_reservations(cfg.state_dir)))


def _project_members(cfg: Config) -> dict[str, frozenset[str]]:
    return {p.key: frozenset(r.repo for r in p.repositories) for p in cfg.projects}


def _bundle_members(cfg: Config, target: Target, issue: int, names=None) -> tuple:
    """The project repositories, beyond the origin's, of the task's scope: the
    recorded one, else `names`."""
    bundle = read_bundle(cfg.state_dir, target.name, issue, repo=target.repo)
    names = {r.repo for r in bundle.repositories} if bundle else set(names or ())
    return tuple(r for p in cfg.projects if p.key == target.name
                 for r in p.repositories if r.repo in names - {target.repo})


def _definition_fields(body: str) -> tuple[list[str], str]:
    """(`Repositories:` names, `Spec repository:` name) of a task definition."""
    field = lambda name: [v.strip() for line in re.findall(  # noqa: E731
        rf"^\s*{name}:\s*(.+)$", body, re.IGNORECASE | re.MULTILINE)
        for v in line.split(",") if v.strip()]
    return field("Repositories"), next(iter(field("Spec repository")), "")


def _declared_scope(cfg: Config, deps: Deps, target: Target, issue: int,
                    dry_run: bool) -> tuple[tuple[str, ...], str]:
    """(member repositories, spec repository) of the task's recorded scope, else
    of its definition (the issue body). ScopeRefused when the definition names a
    repository outside the project."""
    bundle = read_bundle(cfg.state_dir, target.name, issue, repo=target.repo)
    if bundle:
        return tuple(r.repo for r in bundle.repositories), bundle.spec_repo
    if dry_run or not target.sandbox:
        return (), ""
    declared, spec = _definition_fields(deps.github.issue_view(target.repo, issue).get("body") or "")
    if not declared:
        if spec and spec not in _project_members(cfg).get(target.name, frozenset()):
            raise ScopeRefused(f"{target.name}#{issue}: spec repository is not the project's")
        return (), spec
    names = tuple(dict.fromkeys((target.repo, *declared)))
    _require_project_scope(cfg, target.name, issue, names, spec)
    return names, spec


def _require_project_scope(cfg: Config, project: str, issue: int,
                           names: tuple[str, ...], spec: str) -> None:
    if not set(names) <= _project_members(cfg).get(project, frozenset()) \
            or (spec and spec not in names):
        raise ScopeRefused(f"{project}#{issue}: definition scope "
                           f"{sorted(names)} is not the project's")


def _record_bundle(cfg: Config, target: Target, issue: int, names: tuple[str, ...],
                   spec_repo: str, source: str = "definition") -> None:
    """Record the declared scope once its checkouts exist (a recorded one stands)."""
    from dispatcher import sandbox_runtime
    if not names or read_bundle(cfg.state_dir, target.name, issue, repo=target.repo):
        return
    plan = sandbox_runtime.for_clone(target.clone_path)._plan_for(
        sandbox_runtime.execution_ref(target.clone_path, issue))
    save_bundle(cfg.state_dir, target.name, issue, TaskBundle(
        tuple(TaskRepository(r, plan.checkouts[r], f"agent/task-{issue}") for r in names),
        source=source, spec_repo=spec_repo), repo=target.repo)


def _exploration_handover(turn: _Turn, task: TaskState, stage: Stage) -> bool:
    try:
        _establish_exploration_scope(turn, task, stage)
        return True
    except Exception as exc:
        _park_for_input(turn.cfg, turn.deps, turn.target, task,
                        f"Cannot establish exploration repository scope: {exc}")
        return False


def _handover_spec_files(task: TaskState, artifact: str, destination: str) -> None:
    """Carry the spec, and any tickets, written in the origin checkout into the
    spec/plan checkout.

    Existing destination files stand; no checkout content is overwritten.
    """
    from dispatcher.artifacts import SPEC_FOLDER_FILES, ticket_files

    relative = spec_publish.relative_artifact(task.worktree, artifact)
    if relative is None or ".." in Path(relative).parts:
        raise ScopeRefused("explored spec is outside the origin checkout")
    try:
        (Path(task.worktree) / relative).resolve().relative_to(Path(task.worktree).resolve())
    except ValueError as exc:
        raise ScopeRefused("explored spec is outside the origin checkout") from exc
    folder = Path(relative).parent
    for name in SPEC_FOLDER_FILES:
        _copy_absent(Path(task.worktree) / folder / name, destination, str(folder))
    for source in ticket_files(Path(task.worktree) / TICKETS_DIR):
        _copy_absent(source, destination, TICKETS_DIR)


def _copy_absent(source: Path, destination: str, subdir: str) -> None:
    """Copy a readable regular file to <destination>/<subdir> unless one is there."""
    from dispatcher.artifacts import PLAN_FILE_MAX_BYTES
    from dispatcher.state import read_regular
    from dispatcher.workspace import make_worktree_dir

    data = read_regular(source, PLAN_FILE_MAX_BYTES)
    if data is not None and not os.path.lexists(Path(destination) / subdir / source.name):
        make_worktree_dir(destination, subdir)
        write_worktree_file(destination, subdir, source.name, data)


def _establish_exploration_scope(turn: _Turn, task: TaskState, stage: Stage) -> None:
    """Accept one initial scope proposal at the exploration handover only.

    Existing scopes stand. Project membership is checked before requesting
    credentials or provisioning; later reports cannot expand execution access.
    """
    if (task.stage is not Stage.SPEC or stage is not Stage.PLAN
            or not turn.target.sandbox or not turn.signal.repositories):
        return
    if read_bundle(turn.cfg.state_dir, task.target, task.issue, repo=task.repo):
        return
    names = tuple(dict.fromkeys((task.repo, *turn.signal.repositories)))
    from dispatcher.state import pending_spec_repo
    spec_repo = pending_spec_repo(turn.cfg.state_dir, task.target, task.issue, repo=task.repo)
    _require_project_scope(turn.cfg, task.target, task.issue, names, spec_repo)
    members = _bundle_members(turn.cfg, turn.target, task.issue, names)
    create_workspace(turn.target, task.issue, credentials=turn.deps.credentials,
                     members=members, establish_scope=True)
    if spec_repo and spec_repo != task.repo:
        from dispatcher import sandbox_runtime
        plan = sandbox_runtime.for_clone(turn.target.clone_path)._plan_for(
            sandbox_runtime.execution_ref(turn.target.clone_path, task.issue))
        _handover_spec_files(task, turn.signal.artifact, plan.checkouts[spec_repo])
    _record_bundle(turn.cfg, turn.target, task.issue, names, spec_repo, source="exploration")


def _provision_workspace(cfg: Config, deps: Deps, target: Target, cand,
                         dry_run: bool) -> str | None:
    """The candidate's workspace, or None after reporting a provisioning
    failure (the board was never claimed, so nothing to release)."""
    try:
        names, spec_repo = _declared_scope(cfg, deps, target, cand.number, dry_run)
        from dispatcher.state import save_pending_spec_repo
        save_pending_spec_repo(cfg.state_dir, target.name, cand.number, spec_repo, repo=target.repo)
        members = _bundle_members(cfg, target, cand.number, names)
        wt = create_workspace(target, cand.number, dry_run=dry_run,
                              credentials=deps.credentials,
                              **({"members": members} if members else {}))
        _record_bundle(cfg, target, cand.number, names, spec_repo)
        return wt
    except Exception:
        _report_provisioning_failure(cfg, deps, target, cand, dry_run)
        return None


def _claim_once(cfg: Config, deps: Deps, target: Target, cand, launch,
                enabled: list[str], dry_run: bool, pass_started: str = "") -> str:
    """One locked claim decision over the tasks now on disk, so simultaneous
    passes cannot oversubscribe the last place. Returns "claimed", "skip"
    (candidate taken), "drop" (target leaves the round: no place for it, or one
    provisioning failure per target per pass so a systemic fault cannot fail
    every Ready candidate) or "stop" (no execution slot)."""
    with claim_lock(cfg.state_dir):
        now_tasks = load_all(cfg.state_dir)
        repo = _task_repo(cfg, target, cand)
        existing = (load(cfg.state_dir, target.name, cand.number, repo=repo)
                    or load(cfg.state_dir, target.name, cand.number))
        if existing is not None and not _reopened(existing, pass_started):
            return "skip"  # a simultaneous pass claimed this candidate first
        if _claim_denied(cfg, target.name, now_tasks, enabled):
            return "drop"
        slot = allocate_slot(now_tasks, max_slots(cfg.capacity),
                             reserved=state.reserved_slots(cfg.state_dir))
        if slot is None:
            return "stop"
        if existing is not None:   # re-read under the lock: never a fresh claim
            delete(cfg.state_dir, target.name, cand.number, repo=existing.repo)
            eventlog.append_event(cfg.state_dir, "reopened",
                                  target=target.name, issue=cand.number,
                                  detail="canceled task ranked again — "
                                         "reopened by operator")
        wt = _provision_workspace(cfg, deps, target, cand, dry_run)
        if wt is None:
            return "drop"
        _commit_claim(cfg, deps, target, cand, slot, wt, launch)
    return "claimed"


def _claim_new(cfg: Config, deps: Deps, targets: list[Target],
               admit: Admit, order: Order, dry_run: bool,
               pass_started: str = "") -> None:
    """Claim free units one at a time, each via claims.pick_target. A target leaves
    the round when its candidates run out or provisioning fails for it."""
    all_tasks = load_all(cfg.state_dir)
    free = claims.claim_room(cfg.capacity, cfg.max_open, all_tasks,
                             triage_running=False, held=_held(cfg))
    if free <= 0:
        return
    counts = _round_counts(targets, all_tasks)
    last = claims.last_claims(cfg.state_dir)
    caps = {t.name: t.max_active for t in targets}
    # Generator bodies run on first next(), so a target's rank_cmd runs only
    # when it gets a turn.
    gens = {t.name: _claimable(cfg, deps, t,
                               [x for x in all_tasks if x.target == t.name],
                               admit, order, pass_started)
            for t in targets}
    in_round = {t.name: t for t in targets}  # insertion order = list order
    enabled = [p.key for p in cfg.projects if p.enabled]
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
        target = _exec_target(cfg, target, cand)
        outcome = _claim_once(cfg, deps, target, cand, launch, enabled, dry_run,
                              pass_started)
        if outcome == "stop":
            return
        if outcome == "drop":
            del in_round[name]
        if outcome != "claimed":
            continue
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
        return load(cfg.state_dir, intent.target, intent.issue,
                    repo=intent.repo)
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
                   intent.actor or "operator", intent.repo)
    if task is not None:
        _wake_for_queued_message(cfg, deps, task)


def _wake_for_queued_message(cfg: Config, deps: Deps, task: TaskState) -> None:
    """Wake a task that waits for the operator, so its session reads the
    message just queued. Any other task reads it on its next resume."""
    if not task.park and task.stage is Stage.AWAITING_PLAN_REVIEW:
        # A gate task that has not parked yet waits for exactly this message,
        # and only a resume hands the queue to its session: without the wake
        # the grace park would end the session with the message undelivered.
        # So it parks now, as the grace park would (session ended, slot
        # freed), and wakes below.
        _end_session(cfg, deps, task.target, task.issue, task.repo)
        task = replace(task, park=PARK_REVIEW, slot=NO_SLOT)
    if task.park in (PARK_HUMAN, PARK_REVIEW):
        task = loops.reset(task, ResetCause.OPERATOR_WAKE)
        save(cfg.state_dir, replace(task, park=PARK_WAKE, updated_at=_now()))


def _apply_answers_intent(cfg: Config, deps: Deps, task: TaskState | None,
                          intent: intents.Intent) -> None:
    """Write the answers file of the task's open request; a submission
    then wakes the session to read it."""
    reason = answers.drop_reason(task, intent)
    if reason:
        raise IntentDropped(reason)
    assert task is not None
    wake = answers.record(task, intent)
    if wake:
        _queue_message(cfg, task.target, task.issue, wake,
                       intent.actor or "operator", task.repo)
        _wake_for_queued_message(cfg, deps, task)


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
    _park_for_input(cfg, deps, target, task, note="parked by operator",
                    reason=WHY_OPERATOR)


def _release_killed_task(cfg: Config, deps: Deps, by_name: dict,
                         task: TaskState, issue: int) -> None:
    target = by_name.get(task.target)
    if target is not None:
        if task.repo:
            target = replace(target, repo=task.repo)   # the issue lives in ITS repository
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
    _end_session(cfg, deps, kill_target, issue,
                 intent.repo or (task.repo if task is not None else ""))
    if task is not None:
        _release_killed_task(cfg, deps, by_name, task, issue)
    eventlog.append_event(cfg.state_dir, "failed", target=kill_target,
                          issue=issue, repo=intent.repo or (task.repo if task is not None else ""),
                          stage=task.stage.value if task is not None else "",
                          actor=intent.actor, detail="killed by operator")


def _cancel_board_issue(deps: Deps, by_name: dict, target_name: str,
                        issue: int, repo: str = "") -> None:
    target = by_name.get(target_name)
    if target is None:
        log.warning("cancel intent for %s/#%d: target left the config "
                    "— board/issue untouched", target_name, issue)
        return
    if repo:
        target = replace(target, repo=repo)   # the issue lives in ITS repository
    try:
        deps.github.cancel(target, issue)
    except Exception:
        log.warning("github cancel failed for %s/#%d", target_name, issue,
                    exc_info=True)


def _cancel_identity_known(cfg: Config, deps: Deps, by_name: dict, target_name: str,
                           issue: int, repo: str, task: TaskState | None) -> bool:
    if task is not None:
        return (task.target, task.issue, task.repo) == (target_name, issue, repo)
    target = by_name.get(target_name)
    if target is None:
        return False
    # Old unqualified intents retain their single-repository target contract.
    if not repo:
        return True
    # The target view carries only one member repository. Queue identity
    # validation must consult the whole project's verified board ranking.
    source = next((p for p in cfg.projects if p.key == target_name), target)
    return any((row.get("repo", target.repo), row["number"]) == (repo, issue)
               for row in deps.github.rank_rows(source))


def _apply_cancel_intent(cfg: Config, deps: Deps, by_name: dict,
                         task: TaskState | None, intent: intents.Intent) -> None:
    issue = intent.issue
    cancel_target = intent.target or (task.target if task is not None else "")
    if not cancel_target:
        log.warning("cancel intent for #%d: no unique matching task — skipped",
                    issue)
        return
    repo = intent.repo or (task.repo if task is not None else "")
    if not _cancel_identity_known(cfg, deps, by_name, cancel_target, issue, repo, task):
        raise IntentDropped("cancel identity is not a task or current project queue issue")
    _end_session(cfg, deps, cancel_target, issue, repo)
    _cancel_board_issue(deps, by_name, cancel_target, issue, repo)
    if task is not None:
        save(cfg.state_dir, replace(task, stage=Stage.CANCELED, park="",
                                    hold_for_attach=False, slot=NO_SLOT,
                                    operator_request=None, updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "canceled", target=cancel_target,
                          issue=issue, repo=repo, stage=task.stage.value if task else "",
                          actor=intent.actor, detail="canceled by operator")


def _apply_retry_intent(cfg: Config, issue: int, target_name: str = "",
                        repo: str = "") -> None:
    """Clear the quarantine of exactly the named task. A legacy intent (no
    target) clears every target's unqualified record of that number."""
    names = [target_name] if target_name else [t.name for t in cfg.targets]
    for name in names:
        qp = failures.quarantine_path(cfg.state_dir, name, issue, repo)
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
    policy = policy_for(cfg, target and _task_target(cfg, target, task))
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


def _apply_spec_repo_intent(cfg: Config, task: TaskState | None,
                            intent: intents.Intent) -> None:
    """Record the operator's spec/plan repository; one outside the project is refused.
    A stage held for the choice (_held_for_spec_repo) then wakes in that repository."""
    try:
        bundle = select_spec_repo(cfg.state_dir, intent.target, intent.issue,
                                  str(intent.payload.get("spec_repo") or ""),
                                  repo=task.repo if task else intent.repo,
                                  members=_project_members(cfg).get(intent.target, ()))
    except ScopeRefused as refusal:
        print(f"[warn] spec-repo intent for #{intent.issue}: {refusal}", file=sys.stderr)
        return
    if task is not None:
        _after_spec_repo_choice(cfg, task, bundle)


def _after_spec_repo_choice(cfg: Config, task: TaskState, bundle: TaskBundle) -> None:
    owner = next(r for r in bundle.repositories if r.repo == bundle.spec_repo)
    if task.spec_path and owner.worktree != task.worktree:
        _carry_to_spec_repo(task, owner)
    if task.park == PARK_HUMAN and task.stage is Stage.SPEC:
        _wake(cfg, task, f"The operator chose {owner.repo} as the spec/plan repository. Continue.")


def _carry_to_spec_repo(task: TaskState, owner: TaskRepository) -> None:
    """Spec and tickets written in the origin before the choice move to the
    chosen checkout, and the spec is published there; the origin keeps its copy."""
    _handover_spec_files(task, task.spec_path, owner.worktree)
    pub = spec_publish.ensure_published(
        worktree=owner.worktree, branch=owner.branch, repo=owner.repo, issue=task.issue,
        artifact=spec_publish.relative_artifact(task.worktree, task.spec_path) or task.spec_path)
    if pub.error:
        print(f"[warn] spec of #{task.issue} not published in {owner.repo}: {pub.error}",
              file=sys.stderr)


def _apply_required_pr_intent(cfg: Config, deps: Deps, by_name: dict,
                               task: TaskState | None, intent: intents.Intent) -> None:
    if task is None:
        raise ValueError("required-PR command needs an existing task")
    target = _task_target(cfg, by_name[task.target], task)
    changed = required_pr_commands.change(deps.github, target, task,
                                          intent.action, intent.payload)
    save(cfg.state_dir, changed)


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
        _apply_retry_intent(cfg, issue, intent.target, intent.repo)
    elif intent.action == "resume":
        _apply_resume_intent(cfg, by_name, task, intent)
    elif intent.action == "answers":
        _apply_answers_intent(cfg, deps, task, intent)
    elif intent.action in ("replace-pr", "drop-pr", "depend-pr"):
        _apply_required_pr_intent(cfg, deps, by_name, task, intent)
    elif intent.action == "spec-repo":
        _apply_spec_repo_intent(cfg, task, intent)
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
    by_name = {t.name: t for t in [*cfg.targets,
                                   *map(project_target, cfg.projects)]}
    pending = answers.without_superseded(intents.list_intents(cfg.state_dir),
                                         lambda i: _task_for_intent(cfg, i))
    for intent in pending:
        try:
            _apply_one_intent(cfg, deps, by_name, intent)
            eventlog.append_event(cfg.state_dir, "intent-applied",
                                  target=_intent_target(cfg, intent),
                                  issue=intent.issue, actor=intent.actor,
                                  detail=intent.action, repo=intent.repo)
        except IntentDropped as dropped:
            print(f"[warn] intent {intent.path.name}: {dropped}",
                  file=sys.stderr)
            eventlog.append_event(cfg.state_dir, "intent-dropped",
                                  target=_intent_target(cfg, intent),
                                  issue=intent.issue, model=dropped.model,
                                  actor=intent.actor, detail=str(dropped),
                                  repo=intent.repo)
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
    live = {f"task-{task_key(t.target, t.issue, t.repo)}.txt" for t in in_flight} \
         | {f"task-{t.target}-{t.issue}.txt" for t in in_flight} \
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
    (park == PARK_WAKE) and _spawn_feedback's (_awaiting_feedback_round).
    Anything else is not waiting for anything."""
    return task.park == PARK_WAKE or _awaiting_feedback_round(task)


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
        known.add(_wake_blocked_path(cfg, task.target, task.issue, task.repo).name)
        if not holds_slot(task) and task.slot != NO_SLOT:
            eventlog.append_event(cfg.state_dir, "slot-reclaimed",
                                  target=task.target, issue=task.issue, repo=task.repo,
                                  stage=task.stage.value,
                                  detail=f"stage {task.stage.value}"
                                         + (f" park {task.park}" if task.park
                                            else " unparked")
                                         + f" released slot {task.slot}")
            task = replace(task, slot=NO_SLOT)
            save(cfg.state_dir, task)
        if not _starving(task):
            _clear_wake_blocked(cfg, task.target, task.issue, task.repo)
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
        _settle_sandboxes(cfg, deps, dry_run)
        triage.tick(cfg, deps, config_path)
    # Evaluate each triage signal once — triage.running() is a real herdr
    # subprocess; calling it per target would spawn (1 + len(targets)) processes
    # each pass. The two signals are semantically distinct: eff is reduced when
    # the sweep is running (it holds a real capacity unit active() cannot see);
    # claims are paused when any request is in flight (pending = file OR running).
    sweep_running = triage.running()
    _account_allocations(cfg, deps, dry_run, sweep_running)
    # Every project, paused or not, runs its existing work; only the claim
    # round below skips a paused one.
    eff = replace(cfg, targets=[*cfg.targets, *map(project_target, cfg.projects)])
    if sweep_running:
        eff = replace(eff, capacity=max(0, cfg.capacity - 1))
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
    # Phase 1: work already in progress, on every target, before any claim —
    # so one target's new claims never starve another's approved spec.
    for target in eff.targets:
        for task in [t for t in load_all(cfg.state_dir)
                     if t.target == target.name and not t.park
                     and t.stage in IN_FLIGHT_STAGES]:
            run_target = _task_target(eff, target, task)
            try:
                _drive_task(eff, deps, run_target, task, admit, order, dry_run)
            except Exception:
                _fail_task_crash(eff, deps, run_target, task, dry_run)
        _wake_ci(eff, deps, target)
        _poll_prs(eff, deps, target, dry_run)
    _settle_sandboxes(cfg, deps, dry_run)   # stop what phase 1 retired before admitting wakes
    _resume_woken(eff, deps, admit, order, dry_run)
    _spawn_feedback(eff, deps, admit, order, dry_run)
    _settle_sandboxes(cfg, deps, dry_run)   # a task parked this pass frees its unit only once stopped
    # Phase 2: new claims, with whatever capacity phase 1 left.
    if not claims_paused:
        paused = {p.key for p in cfg.projects if not p.enabled}
        _claim_new(eff, deps, [t for t in eff.targets if t.name not in paused],
                   admit, order, dry_run, pass_started)
    _sync_artifacts(cfg, dry_run=dry_run)
    _flush_finished(cfg, dry_run, deps)
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


def _load(config_path: str) -> tuple[Config, Config]:
    """targets.yaml as loaded, and that with the console's stored model
    settings in force; warns when the stored ones are rejected."""
    boot = load_config(config_path)
    cfg = apply_stored_models(boot)
    if cfg.models_origin.error:
        print(f"[warn] {cfg.models_origin.error}", file=sys.stderr)
    return boot, cfg


def _clone_map(cfg: Config) -> dict[str, str]:
    """Clone path by target name and by repository, project members included."""
    return {**{t.name: t.clone_path for t in cfg.targets},
            **{t.repo: t.clone_path for t in cfg.targets},
            **{r.repo: r.clone_path for p in cfg.projects for r in p.repositories}}


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
    missing = referenced_providers(cfg) - set(ADAPTERS)
    if missing:
        print(f"[warn] models: provider(s) {sorted(missing)} have no usage adapter; "
              f"their entries are never admitted", file=sys.stderr)
    credentials = configured_credentials(cfg)
    deps = Deps(github=GitHubClient(dry_run=args.dry_run, credentials=credentials),
                credentials=credentials, sandbox=BoxSandboxes(),
                sessions=Sessions(dry_run=args.dry_run, memory=cfg.session_memory,
                                  cpus=cfg.session_cpus, state_dir=cfg.state_dir,
                                  clones=_clone_map(cfg), credentials=credentials,
                                  sandbox_clones=[r.clone_path for p in cfg.projects
                                                  for r in p.repositories],
                                  members=_project_members(cfg)),
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
            # The console stores model settings under this lock: read them
            # again inside it, so a pass never runs a block the console has
            # replaced (a removed track would park the tasks it claims).
            deps.sessions.renew_tokens()
            guarded_pass(apply_stored_models(boot), deps, args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
