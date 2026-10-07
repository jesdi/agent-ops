"""Edge cases of the answers intent the acceptance file does not reach."""

import shutil

from dispatcher import eventlog, intents
from dispatcher.state import (NO_SLOT, PARK_REVIEW, PARK_WAKE,
                              PlanApprovalRequest, Stage)

from tests.test_answers_intent_acceptance import (ISSUE, PLAN_FILE, REV,
                                                  _drain, _events, _file,
                                                  _gate, _intent, _setup,
                                                  _task, _texts)
from tests.test_main import make_task


def test_an_unknown_submit_value_is_dropped(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, submit="merge")

    _drain(c)

    assert _file(wt) is None
    (e,) = _events(c, "intent-dropped")
    assert "unknown submit" in e["detail"]


def test_a_garbage_file_on_disk_does_not_block_a_draft(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    (wt / PLAN_FILE).write_text("not json")
    _intent(c, {"format": "a"})

    _drain(c)

    assert _file(wt)["answers"] == {"format": "a"}
    assert [e["event"] for e in eventlog.read_tail(c.state_dir)] == ["intent-applied"]


def test_a_request_without_a_revision_matches_no_intent(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c, revision="")
    _intent(c, {"format": "a"}, revision="")

    _drain(c)

    assert _file(wt) is None
    (e,) = _events(c, "intent-dropped")
    assert "stale revision" in e["detail"]


def test_an_intent_for_an_unknown_task_is_dropped(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _intent(c, {"format": "a"})

    _drain(c)

    (e,) = _events(c, "intent-dropped")
    assert "no open request" in e["detail"]


def test_a_submission_while_the_wake_is_queued_is_session_busy(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, submit="changes", ms=1)
    _drain(c)
    _intent(c, {"format": "b"}, submit="approve", ms=5)

    _drain(c)

    assert _file(wt)["submitted"] == "changes"
    (e,) = _events(c, "intent-dropped")
    assert "session busy" in e["detail"]


def test_a_stale_approval_does_not_cost_a_current_draft(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, ms=1)
    _intent(c, {"format": "old"}, submit="approve", revision="rev-0", ms=5)

    _drain(c)

    f = _file(wt)
    assert (f["answers"], f["submitted"]) == ({"format": "a"}, None)
    (e,) = _events(c, "intent-dropped")
    assert "stale revision" in e["detail"]
    assert len(_events(c, "intent-applied")) == 1
    assert _texts(c) == []
    assert intents.list_intents(c.state_dir) == []


def test_a_missing_agent_dir_fails_the_intent_and_creates_nothing(tmp_path, monkeypatch, capsys):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    shutil.rmtree(wt / ".agent")
    _intent(c, {"format": "a"})

    _drain(c)

    assert not (wt / ".agent").exists()
    assert intents.list_intents(c.state_dir) == []
    assert not _events(c, "intent-applied")
    assert "failed" in capsys.readouterr().err


def test_a_submission_resets_the_loop_counters(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    make_task(c, issue=ISSUE, stage=Stage.AWAITING_PLAN_REVIEW, slot=NO_SLOT,
              park=PARK_REVIEW, review_rounds=1, gate_rounds=2, e2e_rounds=1,
              ci_rounds=3, operator_request=PlanApprovalRequest(
                  path=".agent/review.html", fingerprint=REV))
    _intent(c, {"format": "a"}, submit="changes")

    _drain(c)

    t = _task(c)
    assert t.park == PARK_WAKE
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (0, 0, 0, 0)


def test_an_agent_path_that_is_a_file_fails_the_intent(tmp_path, monkeypatch, capsys):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    shutil.rmtree(wt / ".agent")
    (wt / ".agent").write_text("x")
    _intent(c, {"format": "a"})

    _drain(c)

    assert (wt / ".agent").read_text() == "x"
    assert not _events(c, "intent-applied")
    assert "not a directory" in capsys.readouterr().err
