"""Starting, continuing and parking a task's session: the stage prompt with
its queued operator mail, the stage.json `working` signal and its hand-over,
identity healing, task tokens, login and input parks, wakes, and the
notices that name a task."""
from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone

from dispatcher import (eventlog, github_app, implementation_grants, loops, messages, relogin,
                        spec_publish, task_tokens)
from dispatcher.artifacts import TICKETS_DIR
from dispatcher.config import Config, Repository
from dispatcher.deps import Deps, _cursor_now, _now
from dispatcher.launch_choice import Launch, _display_entry, _log_model, _policy
from dispatcher.lifecycle import PR_ATTENTION
from dispatcher.loops import ResetCause
from dispatcher.models import pick_key, tracks_text
from dispatcher.prompts import render_stage_prompt
from dispatcher.state import (NO_POKES, NO_SLOT, Stage, StageSignal, TaskState, at_gate,
                              clear_turn_markers, load_all, park, read_session, read_stage_doc,
                              read_stage_signal, save, stage_signal, task_key, unpark, wake,
                              _clear_wake_blocked, _wake_blocked_path)
from dispatcher.status import Cause, Kind, Status, occupies
from dispatcher.workspace import exclude_local_state, write_task_identity, write_worktree_file
from telegram.templates import task_ref


def _url(repository: Repository, issue: int) -> str:
    return f"https://github.com/{repository.repo}/issues/{issue}"



def _queue_message(cfg: Config, repository: str, issue: int, text: str,
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
    messages.append(cfg.state_dir, repository, issue, text, actor)



def _message_block(msgs: list[messages.Message]) -> str:
    """The block appended to a spawn/resume prompt, oldest first. Actor and
    timestamp are included so the agent can tell an operator instruction from
    a dispatcher-authored CI note."""
    if not msgs:
        return ""
    lines = ["## Operator messages", ""]
    lines += [f"- [{m.created_at}] {m.actor}: {m.text}" for m in msgs]
    return "\n".join(lines)



def _drain(cfg: Config, repository: str, issue: int) -> tuple[str, list[str]]:
    """(block, ids) for everything queued on this issue. The caller stamps
    the ids only AFTER the session actually took the prompt, so a spawn that
    raises leaves the mail queued for the next attempt. Ids are captured
    here, not re-derived later, so a message that arrives during the spawn is
    not stamped as delivered without ever being shown."""
    msgs = messages.undelivered(cfg.state_dir, repository, issue)
    return _message_block(msgs), [m.id for m in msgs]



def _wake(cfg: Config, task: TaskState, text: str, hold: bool = False,
          actor: str = "dispatcher") -> None:
    _queue_message(cfg, task.repository, task.issue, text, actor)
    task = loops.reset(task, ResetCause.OPERATOR_WAKE)
    save(cfg.state_dir, replace(wake(task, Cause.REPLY, hold=hold), updated_at=_now()))



def _signal_working_again(cfg: Config, task: TaskState) -> None:
    """Rewrite a stopped session's stage signal as `working`, so the pass
    after a login code does not re-park the session it re-authenticated."""
    signal = read_stage_signal(task.worktree)
    if signal is None or signal.status == "working":
        return
    by_name = {t.name: t for t in cfg.repositories}
    model = _display_entry(cfg, by_name.get(task.repository), task)
    try:
        write_worktree_file(task.worktree, ".agent", "stage.json", json.dumps(
            {"stage": task.stage.value, "status": "working", "model": model}))
    except OSError as exc:
        # Not written: the next pass reads the old signal and parks again.
        print(f"[warn] stage signal of #{task.issue} not rewritten: {exc}",
              file=sys.stderr)



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

    Un-parking then restores the same invariant a wake's launch and _retry_stage
    enforce — no waiting marker, a `working` signal — or the very next pass
    re-reads blocked/awaiting-ci, re-parks, and ENDS the session that was
    just re-authenticated."""
    if relogin.classify_login(
            deps.sessions.capture_tail(task.repository, task.issue)) is None:
        deps.notifier.send("status", lines=[
            f"{_ref(cfg, task)} is no longer at a login prompt — code NOT typed "
            f"(the pane would have run it as a shell command). Still parked; "
            f"attach to the session to sort it out."])
        return
    deps.sessions.send_text(task.repository, task.issue, code.strip())
    clear_turn_markers(cfg.state_dir, task.repository, task.issue)
    _signal_working_again(cfg, task)
    save(cfg.state_dir, replace(unpark(task), updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "login-code-injected",
                          repository=task.repository, issue=task.issue,
                          stage=task.stage.value, detail="login code injected")



def _ref(cfg: Config, task: TaskState) -> str:
    return task_ref(task.repository, task.issue, len(cfg.repositories) > 1)



def _notify(deps: Deps, repository: Repository, task: TaskState, template: str,
            note: str = "") -> None:
    deps.notifier.send(template, issue=task.issue, title=task.title,
                       url=_url(repository, task.issue), note=note,
                       repository=repository.name)



def _heal_identity(task: TaskState) -> None:
    """Before a launch or a resume: .agent/task.json is rewritten from the
    task's own state, whatever the file held. A failure is logged and the
    launch goes on."""
    try:
        write_task_identity(task.worktree, task.issue, task.repository, task.branch)
    except OSError as exc:
        print(f"[warn] task identity not written for #{task.issue}: {exc}",
              file=sys.stderr)



def _heal_exclude(repository: Repository, task: TaskState) -> None:
    """Before a launch or a resume: a worktree made before the exclude lines
    existed gets them now. The path is the repository's configured clone, never
    one taken from the worktree. The lines are a protection, not a
    precondition for a task that already exists: a failure is logged and the
    launch goes on."""
    try:
        exclude_local_state(repository.clone_path)
    except OSError as exc:
        print(f"[warn] exclude lines not written for #{task.issue}: {exc}",
              file=sys.stderr)



def _write_working(worktree: str, stage: Stage, entry) -> None:
    """Rewrite .agent/stage.json to `working` BEFORE a session is launched or
    resumed, or the next pass reads the old signal again (blocked, a rejected
    report, the previous stage's `done`) and acts on it a second time, and
    log the launch. Raises OSError when .agent is not a usable directory: the
    launch then fails with that reason, and nothing is written outside the
    worktree."""
    _signal_working(worktree, stage, entry)
    _log_model(worktree, stage, str(entry))



def _signal_working(worktree: str, stage: Stage, entry, handover: dict | None = None) -> None:
    """.agent/stage.json says `working` on `stage`. An implement session's
    progress note is kept for the same stage: the tickets it counts are
    still merged on the branch, and the console shows them until the session
    reports again. Never across stages."""
    doc = {"stage": stage.value, "status": "working", "model": entry.model_id,
           "effort": entry.effort}
    old = read_stage_signal(worktree)
    if (stage is Stage.IMPLEMENT and old is not None and old.note
            and (old.stage, old.status) == (stage.value, "working")):
        doc["note"] = old.note
    if handover is not None:
        doc[HANDOVER] = handover
    write_worktree_file(worktree, ".agent", "stage.json", json.dumps(doc))



# The report a launch replaced in stage.json, kept beside its `working`.
HANDOVER = "handover"



def _write_launching(worktree: str, stage: Stage, entry) -> None:
    """_write_working for a launch of the table, before the session can
    report: the report it replaces rides along as the hand-over, which the
    pass after a dispatcher killed before the commit acts on again
    (_handed_over), so the launch is repeated, never lost."""
    old = read_stage_doc(worktree)
    _signal_working(worktree, stage, entry, old.get(HANDOVER, old) if old else None)
    _log_model(worktree, stage, str(entry))



def _handed_over(task: TaskState, doc: dict | None) -> StageSignal | None:
    """The report a launch replaced, while that launch never committed:
    stage.json names the launched stage, the task still the one before."""
    handover = doc.get(HANDOVER) if doc else None
    if not isinstance(handover, dict) or doc.get("stage") == task.stage.value:
        return None
    return stage_signal(handover)



def _signal_of(task: TaskState) -> StageSignal | None:
    return _handed_over(task, read_stage_doc(task.worktree)) or read_stage_signal(task.worktree)



def _with_implement_provider(task: TaskState, stage: Stage, entry) -> list[str]:
    """The providers that ran implement, with `entry`'s when it launches an
    implement session: review reads them (models.review_avoid)."""
    providers = task.implement_providers
    if stage is Stage.IMPLEMENT and entry.provider not in providers:
        providers = [*providers, entry.provider]
    return providers



def _spawn_stage(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                 launch: Launch, spec_path: str = "", message: str = "", *,
                 gh_config: str) -> TaskState:
    # Logical stage boundaries reset loop budgets; a replacement conversation
    # calls the same launch I/O without creating a new allowance.
    task = loops.reset(task, ResetCause.STAGE_STARTED)
    return _launch_stage(cfg, deps, repository, task, launch, spec_path, message,
                         gh_config=gh_config)



def _task_token(cfg: Config, deps: Deps, repository: Repository,
                task: TaskState) -> str | None:
    """The gh config a launch of `task` mounts (`gh_config` of every launch):
    its token directory, never the host's own gh config. None: it can't launch
    this pass. An App error leaves the task as it is, for the next pass (a
    claimed task that never launched waits as waking(launch), which the next
    pass's drive launches); a repository the installation doesn't reach parks
    it for access (_wake_reached wakes it). The one place a launch asks for its
    token, before the launch changes anything."""
    try:
        return task_tokens.issue(deps.access, cfg.state_dir,
                                 task_key(task.repository, task.issue), repository.repo)
    except github_app.AppError as exc:
        print(f"[warn] #{task.issue}: GitHub App error, its launch waits for the next pass: {exc}",
              file=sys.stderr)
        if task.stage is Stage.QUEUED and task.status.kind is Kind.RUNNING:
            save(cfg.state_dir, replace(task, status=Status(Kind.WAKING, cause=Cause.LAUNCH)))
        return None
    except github_app.NoAccess as exc:
        _park_for_input(cfg, deps, repository, task,
                        Status(Kind.PARKED_ACCESS, note=f"{exc}: {exc.link}"))
        return None



def _sync_task_tokens(cfg: Config, deps: Deps) -> None:
    """A task's token directory lives while the task holds a session
    (task_tokens.sync); a refresh that fails waits for the next pass."""
    repos = {r.name: r.repo for r in cfg.repositories}
    holders = {task_key(t.repository, t.issue): repos[t.repository]
               for t in load_all(cfg.state_dir) if occupies(t.status) and t.repository in repos}
    for why in task_tokens.sync(deps.access, cfg.state_dir, holders, deps.clock()):
        print(f"[warn] task token refresh waits for the next pass: {why}", file=sys.stderr)



def _stage_prompt(cfg: Config, repository: Repository, task: TaskState,
                  stage: Stage, spec_path: str, message: str) -> tuple[str, list[str]]:
    """The stage prompt with the caller's message and the queued mail, and
    the ids of that mail (_drain)."""
    ctx = dict(
        issue_number=task.issue, issue_title=task.title,
        issue_url=_url(repository, task.issue), repo=repository.repo,
        branch=task.branch, base=task.base, slot=task.slot,
        backend_port=8100 + task.slot, frontend_port=5200 + task.slot,
        verify_cmd=repository.verify_cmd.format(slot=task.slot),
        gate_cmd=repository.gate_cmd.format(slot=task.slot),
        spec_path=spec_path or task.spec_path,
        tickets_dir=TICKETS_DIR, ticket_count=task.ticket_count,
        pr_number=task.pr_number,
        reason=task.round_reason or "operator",
        labels=", ".join(task.labels),
        tracks=tracks_text(_policy(cfg, repository)),
    )
    prompt = render_stage_prompt(stage, ctx)
    if message:
        prompt = f"{prompt}\n\n{message}"
    block, drained = _drain(cfg, task.repository, task.issue)
    if block:
        prompt = f"{prompt}\n\n{block}\n"
    return prompt, drained



def _spawned(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
             launch: Launch, spec_path: str = "", message: str = "", *,
             gh_config: str) -> TaskState:
    """Spawn `launch.stage`'s session: the task as it now runs, unsaved and
    unstamped. Writes no stage.json, which the caller does."""
    stage, entry = launch.stage, launch.entry
    prompt, drained = _stage_prompt(cfg, repository, task, stage, spec_path, message)
    _heal_exclude(repository, task)
    _heal_identity(task)
    prompt = implementation_grants.prepare(task.worktree, stage.value, entry, launch.second, prompt)
    deps.sessions.spawn_stage(task.repository, task.issue, task.worktree, prompt,
                              stage.value, entry.model_id, entry.effort,
                              second=launch.second, gh_config=gh_config)
    messages.mark_delivered(cfg.state_dir, task.repository, task.issue, drained)
    return replace(unpark(task), stage=stage, spec_path=spec_path or task.spec_path,
                   gate_respawn_pending=(task.gate_respawn_pending and stage is Stage.PLAN
                                         and at_gate(task)),
                   **NO_POKES,
                   picks={**task.picks, pick_key(stage.value): str(entry)},
                   implement_providers=_with_implement_provider(task, stage, entry))



def _stage_started(cfg: Config, task: TaskState, launch: Launch) -> None:
    eventlog.append_event(cfg.state_dir, "stage-started", repository=task.repository,
                          issue=task.issue, stage=launch.stage.value, model=str(launch.entry))



def _launch_stage(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                  launch: Launch, spec_path: str = "",
                  message: str = "", *, gh_config: str) -> TaskState:
    _write_working(task.worktree, launch.stage, launch.entry)
    task = replace(_spawned(cfg, deps, repository, task, launch, spec_path, message,
                            gh_config=gh_config), updated_at=_now())
    save(cfg.state_dir, task)
    _stage_started(cfg, task, launch)
    return task



def _continued(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
               launch: Launch, message: str = "", *, gh_config: str) -> tuple[TaskState, str]:
    """Continue the stage's recorded conversation, else launch the stage
    afresh, saying why. Unsaved, with no stage.json and no event."""
    stage = task.stage
    record = read_session(cfg.state_dir, task.repository, task.issue)
    if record is None or record.stage != stage.value:
        detail = (f"new conversation: session recorded for {record.stage}" if record
                  else "new conversation: no session recorded")
        return _spawned(cfg, deps, repository, task, replace(launch, stage=stage),
                        message=message or "Continue.", gh_config=gh_config), detail
    _heal_identity(task)
    block, drained = _drain(cfg, task.repository, task.issue)
    text = "\n\n".join(filter(None, (message, block))) or "Continue."
    text = implementation_grants.prepare(task.worktree, stage.value, launch.entry, launch.second, text)
    deps.sessions.resume(task.repository, task.issue, task.worktree, text,
                         launch.model, launch.entry.effort,
                         second=launch.second, session_id=record.session_id,
                         gh_config=gh_config)
    messages.mark_delivered(cfg.state_dir, task.repository, task.issue, drained)
    return replace(task, **NO_POKES), ""



def _resumed(cfg: Config, task: TaskState, launch: Launch, detail: str) -> None:
    eventlog.append_event(cfg.state_dir, "resumed", repository=task.repository,
                          issue=task.issue, stage=task.stage.value,
                          model=str(launch.entry), detail=detail)



def _continue_or_restart(cfg: Config, deps: Deps, repository: Repository,
                         task: TaskState, launch: Launch,
                         message: str = "", *, gh_config: str) -> tuple[TaskState, str]:
    """Every continuation names its recorded conversation or launches afresh."""
    task, detail = _continued(cfg, deps, repository, task, launch, message,
                              gh_config=gh_config)
    if detail:
        _write_working(task.worktree, task.stage, launch.entry)
        task = replace(task, updated_at=_now())
        save(cfg.state_dir, task)
        _stage_started(cfg, task, replace(launch, stage=task.stage))
    _resumed(cfg, task, launch, detail)
    return task, detail



def _end_session(cfg: Config, deps: Deps, repository: str, issue: int) -> None:
    """End the task's session and drop its turn-end markers (waiting and
    background): the next session must never inherit the old one's."""
    deps.sessions.end(repository, issue)
    clear_turn_markers(cfg.state_dir, repository, issue)



def _with_tail(note: str, tail: str) -> str:
    """A park question with the session's last screen under it."""
    return (note + ("\n\n" + tail if tail else "")).strip() or "(no detail)"



def _park_for_input(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                    status: Status) -> None:
    """The park of the events the table does not take yet: an unreached
    repository (access). A session's own parks, answers included, and the
    operator's park are the table's."""
    note = status.note
    tail = deps.sessions.capture_tail(task.repository, task.issue)
    login = relogin.classify_login(tail)
    if login is not None and _park_for_login(cfg, deps, repository, task, note,
                                             tail, login):
        return
    msg_id = deps.notifier.send(
        "parked_question", issue=task.issue, title=task.title,
        url=_url(repository, task.issue), repository=repository.name,
        note=_with_tail(note, tail))
    _end_session(cfg, deps, task.repository, task.issue)
    # A wake that ends here is over: the wake that follows this park says
    # for itself whether it is an attach, and on which model.
    parked = park(task, status, msg_id=msg_id)
    save(cfg.state_dir, replace(parked, resume_model_override="",
                                resume_bypass_usage=False,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", repository=repository.name,
                          issue=task.issue, stage=task.stage.value, detail=note)



def _park_for_login(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                    note: str, tail: str, login: relogin.LoginPrompt) -> bool:
    """Unlike every other park, the session is NOT ended: the reply path
    types the OAuth code into this exact pane. Only this task is parked;
    other sessions hitting the shared-claude-home prompt are caught by
    their own stall timers.

    Returns False, and the caller parks, when the task holds no slot (a
    wake, a PR feedback round: held(login) occupies one), or when the ping
    could not be delivered (Notifier.send yields 0 on any send error): a
    park_msg_id of 0 matches no reply, so the task would hold a live
    container forever with no way back in — the generic park at least
    releases it.

    The waiting marker is cleared like in every other park: it says "the
    session stopped mid-stage", and leaving it set makes the pass after the
    un-park re-park (and END) the freshly re-authenticated session. Pane
    liveness is what this park protects, and the marker has no part in it."""
    if task.slot == NO_SLOT:
        return False
    msg_id = deps.notifier.send(
        "needs_relogin", issue=task.issue, title=task.title,
        url=_url(repository, task.issue), repository=repository.name,
        login_url=login.url or "(attach to the session to see the URL)",
        note=(note + ("\n\n" + tail if tail else "")).strip() or "(no detail)")
    if msg_id == 0:
        return False
    clear_turn_markers(cfg.state_dir, task.repository, task.issue)
    save(cfg.state_dir, replace(park(task, Status(Kind.HELD_LOGIN, note=note), msg_id=msg_id),
                                **NO_POKES,
                                updated_at=_now()))
    eventlog.append_event(cfg.state_dir, "parked", repository=repository.name,
                          issue=task.issue, stage=task.stage.value,
                          detail="needs re-login: " + note)
    return True



def _grace_elapsed(cfg: Config, task: TaskState) -> bool:
    """Has the plan-review gate gone unanswered past the grace period?
    `updated_at` was stamped when the task became held(plan-approval) for
    this revision, and only a change of stage or status stamps it again
    (_commit; an armed ready report read again changes neither), so no
    extra timer field is needed and the pass stays stateless. An
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



def _mark_wake_blocked(cfg: Config, repository: Repository, task: TaskState,
                       reason: str) -> None:
    """Edge-triggered: the event fires the pass a wake FIRST goes hungry, not
    every pass thereafter — a resume that waits overnight must not write 144
    identical lines into events.jsonl. The marker is the edge."""
    p = _wake_blocked_path(cfg, task.repository, task.issue)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    eventlog.append_event(cfg.state_dir, "wake-blocked", repository=repository.name,
                          issue=task.issue, stage=task.stage.value,
                          detail=reason)



def _pr_reason(cause: Cause | None) -> str:
    """Why the PR wakes it, as the address-review prompt names the round; any
    other wake at pr-open is the operator's."""
    return next((a.kind for a in PR_ATTENTION.values() if a.cause is cause), "operator")



def _address_review_round(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                          launch: Launch, message: str, gh_config: str) -> TaskState:
    """No session to continue at pr-open: a wake on a pr-open task is a fresh
    address-review round, for the PR's attention or carrying the operator's
    message. Cursor := spawn time: everything the session can read live is
    now "seen"; _cursor_now(), as GitHub timestamps are second-granular and
    pr_poll's trigger is a strict `>`."""
    task = replace(unpark(task), round_reason=_pr_reason(task.status.cause),
                   feedback_cursor=_cursor_now(),
                   resume_model_override="", resume_bypass_usage=False, resume_text="")
    _clear_wake_blocked(cfg, task.repository, task.issue)
    task = _spawn_stage(cfg, deps, repository, task, launch, message=message,
                        gh_config=gh_config)
    eventlog.append_event(cfg.state_dir, "resumed", repository=repository.name,
                          issue=task.issue, stage=Stage.ADDRESS_REVIEW.value,
                          model=str(launch.entry))
    return task



def _continue_woken(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                    launch: Launch, message: str, gh_config: str) -> tuple[TaskState, str]:
    """Continue a woken task's recorded conversation, else launch its stage
    afresh: the task as it now runs, unsaved, and why a new conversation
    replaced the recorded one, else ""."""
    entry = launch.entry
    # Before resuming: the next pass must not re-read blocked/awaiting-ci
    # and re-park the freshly resumed session.
    _write_working(task.worktree, task.stage, entry)
    _log_model(task.worktree, task.stage, str(entry))
    task, detail = _continue_or_restart(cfg, deps, repository, task, launch, message,
                                        gh_config=gh_config)
    _clear_wake_blocked(cfg, task.repository, task.issue)
    return replace(unpark(task),
                   resume_model_override="",
                   resume_bypass_usage=False,
                   resume_text="",
                   picks={**task.picks, pick_key(task.stage.value): str(entry)},
                   updated_at=_now()), detail



RESUME_TEXT = "The operator resumed this task. Continue."



def _resume_text(task: TaskState) -> str:
    """What the launch of an operator's resume tells the session, from the
    wake's own save: nothing is queued between the wake and its launch."""
    if task.status.cause is not Cause.RESUME:
        return ""
    return task.resume_text or RESUME_TEXT
