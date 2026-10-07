"""Edge cases of the answers intent the acceptance file does not reach."""

from dispatcher import eventlog

from tests.test_answers_intent_acceptance import (PLAN_FILE, _drain, _events,
                                                  _file, _gate, _intent, _setup)


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
