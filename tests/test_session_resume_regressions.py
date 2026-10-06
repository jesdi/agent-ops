"""Continuation regressions through the dispatcher and runtime public seams."""
import json
import shlex

import pytest

import dispatcher.main as main
from dispatcher import messages, runtimes
from dispatcher.state import PARK_WAKE, Stage, load, read_session
from tests.test_main import FakeGitHub, deps, make_task, valid_spec, write_tickets
from tests.test_session_resume_acceptance import (
    CLAUDE_MODEL, CODEX_ID, CODEX_MODEL, RecordingSessions, _config, _task,
)


@pytest.mark.parametrize("model", [CLAUDE_MODEL, CODEX_MODEL])
def test_crash_without_a_record_offers_no_conversation_guess(tmp_path, monkeypatch, model):
    c = _config(tmp_path, monkeypatch, model)
    make_task(c, issue=384, stage=Stage.IMPLEMENT, picks={"implement": model})
    github = FakeGitHub()
    main.run_pass(c, deps(gh=github, sess=RecordingSessions(c.state_dir)))
    report = github.created_issues[0][2]
    assert "no session recorded" in report
    assert "codex resume" not in report and "claude --resume" not in report
    assert "--last" not in report and "--continue" not in report


@pytest.mark.parametrize("runtime", [runtimes.CLAUDE, runtimes.CODEX])
def test_runtime_quotes_the_recorded_identifier_as_one_shell_word(runtime):
    session_id = "record '$(touch /tmp/not-executed); id"
    command = shlex.split(runtime.resume_cmd(session_id, shlex.quote("It's safe; go")))
    assert command[-2:] == [session_id, "It's safe; go"]


@pytest.mark.parametrize("model", [CLAUDE_MODEL, CODEX_MODEL])
@pytest.mark.parametrize("stage", [Stage.PLAN, Stage.SPEC])
def test_stage_retry_continues_its_valid_record(tmp_path, monkeypatch, model, stage):
    c = _config(tmp_path, monkeypatch, model)
    wt = _task(c, 384, stage, model=model, park="", slot=0, track="standard")
    if stage is Stage.PLAN:
        tickets = wt / ".agent" / "tickets"
        tickets.mkdir()
        (tickets / "01-bad.md").write_text("# tiny\n")
        signal = {"stage": "plan", "status": "done", "artifact": ".agent/tickets"}
        reason = "mechanical check"
    else:
        valid_spec(wt)
        signal = {"stage": "spec", "status": "awaiting-review", "artifact": "spec.md",
                  "track": "tivial"}
        reason = "tivial"
    (wt / ".agent" / "stage.json").write_text(json.dumps(signal))
    sessions = RecordingSessions(c.state_dir, alive={384})
    main.run_pass(c, deps(sess=sessions))
    (resume,) = sessions.resume_records
    assert resume["session_id"] == CODEX_ID
    assert resume["model"] == model and reason in resume["message"]
    assert sessions.spawned == []
    task = load(c.state_dir, "portfolio_eval", 384)
    assert getattr(task, f"{stage.value}_retries") == 1
    assert read_session(c.state_dir, "portfolio_eval", 384).session_id == CODEX_ID


@pytest.mark.parametrize("model", [CLAUDE_MODEL, CODEX_MODEL])
def test_restart_with_missing_current_ticket_preserves_the_operator_message(
        tmp_path, monkeypatch, model):
    c = _config(tmp_path, monkeypatch, model)
    wt = make_task(c, issue=384, stage=Stage.IMPLEMENT, park=PARK_WAKE,
                   ticket_cursor=2, ticket_count=2, picks={"implement": model})
    write_tickets(wt, 1)
    queued = messages.append(c.state_dir, "portfolio_eval", 384,
                             "Use the existing endpoint", "operator")
    github = FakeGitHub()
    sessions = RecordingSessions(c.state_dir)

    main.run_pass(c, deps(gh=github, sess=sessions))

    assert sessions.spawned == [] and sessions.resume_records == []
    assert messages.undelivered(c.state_dir, "portfolio_eval", 384) == [queued]
    assert load(c.state_dir, "portfolio_eval", 384).stage is Stage.FAILED
    (report,) = github.created_issues
    assert f"ticket 2 of 2 missing under {wt}/.agent/tickets" in report[2]
