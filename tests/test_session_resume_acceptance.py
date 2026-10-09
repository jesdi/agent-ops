"""Ticket 04 acceptance: resumes name the task's recorded conversation.

Public seams only; every CLI/tab interaction is a fake. Invalid-record
fallbacks belong to ticket 05 and are deliberately absent here.
"""
from tests.runtime_listener import launch_listener, seed_resume_task  # noqa: F401

import json
import shlex
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures("launch_listener")


import dispatcher.main as main
from dispatcher import eventlog, herdr, intents, messages, runtimes
from dispatcher.models import parse_policy
from dispatcher.sessions import Sessions
from dispatcher.state import (
    NO_SLOT, PARK_HUMAN, SessionRecord, Stage, load, read_session,
    write_session,
)
from tests.test_containers import make_worktree
from tests.test_main import (
    FakeGitHub, FakeSessions, cfg, deps, make_task, write_tickets,
)
from tests.test_sessions import _fake_podman, herdr_fake_creating
from tests.usagefakes import session_usage
from telegram.inbound import Command


CODEX_ID = "01a0fc74-9f0c-75f3-8b77-a5b5e6dfb884"
OTHER_ID = "01a0fd57-9f0c-75f3-8b77-a5b5e6dfb281"
LIVE_ID = "01a11028-9f0c-75f3-8b77-a5b5e6dfb370"
CLAUDE_ID = "7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d"
CODEX_MODEL = "openai/gpt-6-astra"
CLAUDE_MODEL = "anthropic/claude-opus-5-5"
TARGET = "portfolio_eval"


class RecordingSessions(FakeSessions):
    """Record the proposed public contract without relying on old fake tuples."""

    def __init__(self, state_dir, *, alive=(), launch_real=False):
        super().__init__(alive=alive)
        self.state_dir = state_dir
        self.launch_real = launch_real
        self.resume_records = []

    def resume(self, target, issue, worktree, message, model, effort="",
               second=None, *, session_id=None):
        self.resume_records.append({
            "target": target, "issue": issue, "worktree": worktree,
            "message": message, "model": model, "session_id": session_id,
        })

    def spawn_stage(self, target, issue, worktree, prompt, stage_name, model,
                    effort="", second=None, ticket=""):
        super().spawn_stage(target, issue, worktree, prompt, stage_name,
                            model, effort, second, ticket=ticket)
        if self.launch_real:
            Sessions(state_dir=self.state_dir).spawn_stage(
                target, issue, worktree, prompt, stage_name, model,
                effort=effort, second=second, ticket=ticket)


def _config(tmp_path, monkeypatch, model=CODEX_MODEL):
    policy = parse_policy({
        "triage": ["claude-sonnet-5@low"], "untracked": "standard",
        "tracks": {"standard": {
            "when": "All work.",
            **{stage: [model] for stage in ("spec", "plan", "implement", "review")},
        }},
    })
    c = replace(cfg(tmp_path), models=policy)
    monkeypatch.setattr(main, "fetch_all", lambda *a, **k: {
        provider: session_usage(provider=provider)
        for provider in ("anthropic", "openai")
    })
    return c


def _task(c, issue, stage, *, session_id=CODEX_ID, park=PARK_HUMAN,
          slot=NO_SLOT, model=CODEX_MODEL, **kw):
    continued_stage = "spec" if stage is Stage.AWAITING_SPEC_REVIEW else stage.value
    wt = make_task(c, issue=issue, stage=stage, park=park, slot=slot,
                   picks={continued_stage: model}, **kw)
    (wt / ".git").write_text(
        f"gitdir: {c.targets[0].clone_path}/.git/worktrees/task-{issue}\n")
    write_session(c.state_dir, TARGET, issue,
                  SessionRecord(session_id=session_id, stage=continued_stage))
    return wt


def _reply(c, issue, text, request_id=1):
    intents.write_intent(c.state_dir, "reply", TARGET, issue,
                         {"text": text}, "operator", request_id)


def _tab_fake(monkeypatch, calls, *, target="acme", issue=42, on_start=None):
    """Reuse the stateful fake server and adapt only its task/workspace labels."""
    herdr_fake_creating(monkeypatch, calls)
    original = herdr._run

    def run(args):
        if on_start and args[:2] in (["tab", "create"], ["pane", "run"]):
            on_start(args[:2])
        result = original(args)
        if result is not None:
            result.stdout = result.stdout.replace(
                "task-acme-42", f"task-{target}-{issue}").replace(
                    '"acme"', json.dumps(target))
        return result

    monkeypatch.setattr(herdr, "_run", run)


def test_codex_tab_command_names_the_recorded_id_and_quotes_the_message(
        tmp_path, monkeypatch):
    wt, _ = make_worktree(tmp_path)
    calls = []
    _tab_fake(monkeypatch, calls)
    message = "It's approved; use $(the token) safely"
    quoted = shlex.quote(message)
    assert runtimes.CODEX.resume(CODEX_ID, quoted) == f"resume {CODEX_ID} {quoted}"
    assert runtimes.CODEX.resume_cmd(CODEX_ID, quoted) == f"codex resume {CODEX_ID} {quoted}"
    assert shlex.split(runtimes.CODEX.resume_cmd(CODEX_ID)) == ["codex", "resume", CODEX_ID]
    seed_resume_task(tmp_path, wt)
    Sessions(state_dir=tmp_path).resume("acme", 42, wt, message, CODEX_MODEL, session_id=CODEX_ID)
    command = next(call[3] for call in calls if call[:2] == ["pane", "run"])
    assert " codex " in command
    assert command.endswith(f" resume {CODEX_ID} {quoted}")
    assert "--last" not in command
    assert "--continue" not in command


def test_claude_tab_command_names_the_recorded_id_and_quotes_the_message(
        tmp_path, monkeypatch):
    wt, _ = make_worktree(tmp_path)
    calls = []
    _tab_fake(monkeypatch, calls)
    message = "It's approved; use $(the token) safely"
    quoted = shlex.quote(message)
    assert runtimes.CLAUDE.resume(CLAUDE_ID, quoted) == f"--resume {CLAUDE_ID} {quoted}"
    assert runtimes.CLAUDE.resume_cmd(CLAUDE_ID, quoted) == f"claude --resume {CLAUDE_ID} {quoted}"
    assert shlex.split(runtimes.CLAUDE.resume_cmd(CLAUDE_ID)) == ["claude", "--resume", CLAUDE_ID]
    seed_resume_task(tmp_path, wt)
    Sessions(state_dir=tmp_path).resume("acme", 42, wt, message, CLAUDE_MODEL, session_id=CLAUDE_ID)
    command = next(call[3] for call in calls if call[:2] == ["pane", "run"])
    assert " claude " in command
    assert command.endswith(f" --resume {CLAUDE_ID} {quoted}")
    assert "--continue" not in command
    assert "--last" not in command


def test_operator_approval_resumes_the_recorded_spec_in_its_worktree(
        tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    wt = _task(c, 384, Stage.AWAITING_SPEC_REVIEW)
    _reply(c, 384, "Approved, go on")
    sessions = RecordingSessions(c.state_dir)
    main.run_pass(c, deps(sess=sessions))
    (resume,) = sessions.resume_records
    assert (resume["issue"], resume["session_id"], resume["worktree"], resume["model"]) == (
        384, CODEX_ID, str(wt), CODEX_MODEL)
    assert "Approved, go on" in resume["message"]
    assert messages.undelivered(c.state_dir, TARGET, 384) == []
    assert sessions.spawned == []


def test_operator_reply_resumes_the_recorded_claude_implement_session(
        tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch, CLAUDE_MODEL)
    wt = _task(c, 370, Stage.IMPLEMENT, model=CLAUDE_MODEL,
               session_id=CLAUDE_ID)
    _reply(c, 370, "Use the existing endpoint")
    sessions = RecordingSessions(c.state_dir)
    main.run_pass(c, deps(sess=sessions))
    (resume,) = sessions.resume_records
    assert (resume["issue"], resume["session_id"], resume["worktree"], resume["model"]) == (
        370, CLAUDE_ID, str(wt), CLAUDE_MODEL)
    assert "Use the existing endpoint" in resume["message"]
    assert messages.undelivered(c.state_dir, TARGET, 370) == []
    assert sessions.spawned == []


@pytest.mark.parametrize("reply_order", [(384, 281), (281, 384)])
def test_interleaved_codex_tasks_keep_their_own_id_worktree_and_reply(
        tmp_path, monkeypatch, reply_order):
    c = _config(tmp_path, monkeypatch)
    # #281 records last: recency must have no bearing on either resume.
    worktrees = {
        384: _task(c, 384, Stage.REVIEW, session_id=CODEX_ID),
        281: _task(c, 281, Stage.REVIEW, session_id=OTHER_ID),
    }
    replies = {384: "Use the token", 281: "Rerun the gate"}
    ids = {384: CODEX_ID, 281: OTHER_ID}
    for request_id, issue in enumerate(reply_order, 1):
        _reply(c, issue, replies[issue], request_id)
    sessions = RecordingSessions(c.state_dir)
    main.run_pass(c, deps(sess=sessions))
    assert len(sessions.resume_records) == 2
    for resume in sessions.resume_records:
        issue = resume["issue"]
        sibling = 281 if issue == 384 else 384
        assert (resume["session_id"], resume["worktree"]) == (ids[issue], str(worktrees[issue]))
        assert replies[issue] in resume["message"]
        assert ids[sibling] not in str(resume)
        assert str(worktrees[sibling]) not in str(resume)
        assert replies[sibling] not in resume["message"]
        assert messages.undelivered(c.state_dir, TARGET, issue) == []


def test_resume_leaves_a_live_codex_sibling_and_its_record_untouched(
        tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    _task(c, 370, Stage.REVIEW, session_id=LIVE_ID, park="", slot=0,
          updated_at=(datetime.now(timezone.utc) - timedelta(seconds=11)).isoformat())
    before = read_session(c.state_dir, TARGET, 370)
    wt = _task(c, 384, Stage.REVIEW)
    _reply(c, 384, "Approved")
    sessions = RecordingSessions(c.state_dir, alive={370})
    main.run_pass(c, deps(sess=sessions))
    (resume,) = sessions.resume_records
    assert (resume["issue"], resume["session_id"], resume["worktree"]) == (384, CODEX_ID, str(wt))
    assert (TARGET, 370) not in sessions.end_calls
    assert sessions.is_alive(TARGET, 370)
    assert read_session(c.state_dir, TARGET, 370) == before


def test_attach_with_a_valid_record_resumes_by_id_with_an_empty_note(
        tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    wt = _task(c, 281, Stage.REVIEW, session_id=OTHER_ID)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda state_dir: [Command(name="attach", issue=281)])
    sessions = RecordingSessions(c.state_dir)
    d = deps(sess=sessions)
    main.run_pass(c, d)
    (resume,) = sessions.resume_records
    assert (resume["session_id"], resume["worktree"]) == (OTHER_ID, str(wt))
    (notification,) = [ctx for name, ctx in d.notifier.contexts if name == "resumed_for_attach"]
    assert notification["note"] == ""


def test_resume_by_id_retains_its_record_and_logs_an_empty_detail(
        tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    _task(c, 384, Stage.REVIEW)
    before = read_session(c.state_dir, TARGET, 384)
    _reply(c, 384, "Continue")
    sessions = RecordingSessions(c.state_dir)
    main.run_pass(c, deps(sess=sessions))
    assert sessions.resume_records[0]["session_id"] == CODEX_ID
    assert read_session(c.state_dir, TARGET, 384) == before
    (event,) = [event for event in eventlog.read_tail(c.state_dir)
                if event["event"] == "resumed" and event["issue"] == 384]
    assert event["detail"] == ""


def test_plan_fresh_launch_clears_the_spec_record_before_the_tab_starts(
        tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    wt = _task(c, 384, Stage.AWAITING_SPEC_REVIEW, park="", slot=0)
    (wt / "spec.md").write_text("# t — design\n\n## Problem\n\n" + "x " * 400
                               + "\n\n## Decisions\n\n" + "y " * 400)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "spec", "status": "done", "artifact": "spec.md", "track": "standard",
    }))
    observed = []
    _tab_fake(monkeypatch, [], target=TARGET, issue=384,
              on_start=lambda phase: observed.append((phase, read_session(c.state_dir, TARGET, 384))))
    sessions = RecordingSessions(c.state_dir, alive={384}, launch_real=True)
    main.run_pass(c, deps(sess=sessions))
    assert load(c.state_dir, TARGET, 384).stage is Stage.PLAN
    assert sessions.spawned[0][1] == "plan"
    assert observed and all(record is None for _, record in observed)
    assert observed[0][0] == ["tab", "create"]


def test_ticket_four_fresh_launch_clears_ticket_threes_record_before_the_tab_starts(
        tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    wt = _task(c, 370, Stage.IMPLEMENT, park="", slot=0,
               ticket_cursor=3, ticket_count=4)
    write_tickets(wt, 4)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 3 green",
    }))
    observed = []
    _tab_fake(monkeypatch, [], target=TARGET, issue=370,
              on_start=lambda phase: observed.append((phase, read_session(c.state_dir, TARGET, 370))))
    sessions = RecordingSessions(c.state_dir, alive={370}, launch_real=True)
    main.run_pass(c, deps(sess=sessions))
    assert load(c.state_dir, TARGET, 370).ticket_cursor == 4
    assert "04-t4.md" in sessions.spawned[0][3]
    binding = Sessions(state_dir=c.state_dir).runtime_view(TARGET, 370)["binding"]
    assert binding["stage"] == "implement" and binding["ticket"] == "4"
    assert observed and all(record is None for _, record in observed)
    assert observed[0][0] == ["tab", "create"]


def test_parking_keeps_the_session_record(tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    wt = _task(c, 384, Stage.REVIEW, park="", slot=0)
    before = read_session(c.state_dir, TARGET, 384)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "review", "status": "blocked", "note": "need a token",
    }))
    main.run_pass(c, deps(sess=RecordingSessions(c.state_dir, alive={384})))
    assert load(c.state_dir, TARGET, 384).park == PARK_HUMAN
    assert read_session(c.state_dir, TARGET, 384) == before


def test_session_end_keeps_the_session_record(tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    _task(c, 384, Stage.REVIEW)
    before = read_session(c.state_dir, TARGET, 384)
    _fake_podman(monkeypatch)
    Sessions(state_dir=c.state_dir).end(TARGET, 384)
    assert read_session(c.state_dir, TARGET, 384) == before


def test_flushing_a_task_removes_its_session_record(tmp_path, monkeypatch):
    c = _config(tmp_path, monkeypatch)
    _task(c, 384, Stage.DONE, park="", done_at="2000-01-01T00:00:00+00:00")
    main.run_pass(c, deps(sess=RecordingSessions(c.state_dir)))
    assert load(c.state_dir, TARGET, 384) is None
    assert read_session(c.state_dir, TARGET, 384) is None


@pytest.mark.parametrize("model, session_id, prefix", [
    (CODEX_MODEL, CODEX_ID, "codex resume"),
    (CLAUDE_MODEL, CLAUDE_ID, "claude --resume"),
])
def test_crash_repro_names_the_recorded_conversation(
        tmp_path, monkeypatch, model, session_id, prefix):
    c = _config(tmp_path, monkeypatch, model)
    wt = _task(c, 384, Stage.IMPLEMENT, model=model, session_id=session_id,
               park="", slot=0)
    github = FakeGitHub()
    main.run_pass(c, deps(gh=github, sess=RecordingSessions(c.state_dir)))
    ((repo, _, report),) = github.created_issues
    assert repo == c.targets[0].repo
    assert f"cd {wt} && {prefix} {session_id}" in report
    repro = next(line for line in report.splitlines() if line.startswith("- repro:"))
    assert "--last" not in repro
    assert "--continue" not in repro
