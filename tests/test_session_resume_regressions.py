"""Continuation regressions through the dispatcher and runtime public seams."""
import json
import shlex

import pytest

import dispatcher.main as main
from dispatcher import messages, runtimes
from dispatcher.sessions import Sessions
from dispatcher.state import PARK_WAKE, Stage, load, read_session
from tests.test_main import (REVIEW_PAGE_STUB, FakeGitHub, deps, gate_signal,
                             make_task, valid_spec)
from tests.test_session_resume_acceptance import (
    CLAUDE_MODEL, CODEX_ID, CODEX_MODEL, RecordingSessions, _config, _tab_fake, _task,
)


OPTION_SHAPED_IDS = [
    (CODEX_MODEL, "--last", ["resume", "--", "--last"]),
    (CODEX_MODEL, "--continue", ["resume", "--", "--continue"]),
    (CODEX_MODEL, "--", ["resume", "--", "--"]),
    (CODEX_MODEL, "-h", ["resume", "--", "-h"]),
    (CODEX_MODEL, "--resume=other", ["resume", "--", "--resume=other"]),
    (CODEX_MODEL, "-a '$(not-executed); id", ["resume", "--", "-a '$(not-executed); id"]),
    (CLAUDE_MODEL, "--last", ["--resume=--last"]),
    (CLAUDE_MODEL, "--continue", ["--resume=--continue"]),
    (CLAUDE_MODEL, "--", ["--resume=--"]),
    (CLAUDE_MODEL, "-h", ["--resume=-h"]),
    (CLAUDE_MODEL, "--resume=other", ["--resume=--resume=other"]),
    (CLAUDE_MODEL, "-a '$(not-executed); id", ["--resume=-a '$(not-executed); id"]),
]


@pytest.mark.parametrize("model, session_id, expected_args", OPTION_SHAPED_IDS)
def test_operator_wake_passes_option_shaped_id_literally_to_the_tab(
        tmp_path, monkeypatch, model, session_id, expected_args):
    c = _config(tmp_path, monkeypatch, model)
    _task(c, 384, Stage.REVIEW, model=model, session_id=session_id,
          park=PARK_WAKE, slot=0)
    calls = []
    _tab_fake(monkeypatch, calls, target="portfolio_eval", issue=384)
    messages.append(c.state_dir, "portfolio_eval", 384,
                    "It's approved; use $(the token) safely", "operator")

    class TabSessions(RecordingSessions):
        def resume(self, *args, **kwargs):
            super().resume(*args, **kwargs)
            Sessions(state_dir=c.state_dir).resume(*args, **kwargs)

    sessions = TabSessions(c.state_dir)
    main.run_pass(c, deps(sess=sessions))

    (resume,) = sessions.resume_records
    assert resume["session_id"] == session_id
    assert "It's approved; use $(the token) safely" in resume["message"]
    command = next(call[3] for call in calls if call[:2] == ["pane", "run"])
    expected = [*expected_args, resume["message"]]
    assert shlex.split(command)[-len(expected):] == expected
    assert messages.undelivered(c.state_dir, "portfolio_eval", 384) == []
    assert read_session(c.state_dir, "portfolio_eval", 384).session_id == session_id


@pytest.mark.parametrize("model, session_id, expected_args", OPTION_SHAPED_IDS)
def test_crash_repro_passes_option_shaped_id_literally(
        tmp_path, monkeypatch, model, session_id, expected_args):
    c = _config(tmp_path, monkeypatch, model)
    wt = _task(c, 384, Stage.IMPLEMENT, model=model, session_id=session_id,
               park="", slot=0)
    github = FakeGitHub()
    main.run_pass(c, deps(gh=github, sess=RecordingSessions(c.state_dir)))
    report = github.created_issues[0][2]
    repro = next(line.removeprefix("- repro: `").removesuffix("`")
                 for line in report.splitlines() if line.startswith("- repro:"))
    cli = "codex" if model == CODEX_MODEL else "claude"
    assert shlex.split(repro, comments=True) == ["cd", str(wt), "&&", cli, *expected_args]


@pytest.mark.parametrize("model", [CLAUDE_MODEL, CODEX_MODEL])
def test_crash_without_a_record_offers_no_conversation_guess(tmp_path, monkeypatch, model):
    c = _config(tmp_path, monkeypatch, model)
    make_task(c, issue=384, stage=Stage.IMPLEMENT, picks={"implement": model},
              session=False)
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
        (wt / ".agent" / "review.html").write_text(REVIEW_PAGE_STUB)
        gate_signal(wt)
        reason = "01-bad.md"
    else:
        valid_spec(wt)
        signal = {"stage": "spec", "status": "done", "artifact": "spec.md",
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
