"""Operator intents (web console writes), drained at the top of a pass:
each is routed to its per-kind applier, and the table decides the ones it
owns; a refused or dropped intent is recorded, never retried."""
from __future__ import annotations

import logging
import sys
from dataclasses import replace

from dispatcher import answers, eventlog, failures, intents, task_artifacts
from dispatcher.config import Config, Repository, policy_for
from dispatcher.deps import Deps, _now
from dispatcher.executor import _OPERATOR_ENDS, _Turn, _apply, _cancel_issue
from dispatcher.lifecycle import Event, Reject, decide, OPERATOR_INTENTS, operator_event
from dispatcher.models import override_refusal
from dispatcher.stage_launch import _end_session, _inject_login_code, _queue_message
from dispatcher.state import TaskState, end, load, load_all, next_stage, save
from dispatcher.status import Kind

log = logging.getLogger(__name__)


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
        try:
            task_artifacts.capture_answers(cfg.state_dir, task)
        except Exception as exc:   # the answers and the wake never wait on the archive
            log.warning("Cannot capture the answered page of %s#%s: %s", task.repository, task.issue, exc)
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
