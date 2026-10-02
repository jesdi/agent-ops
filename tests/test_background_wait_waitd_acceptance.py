"""Acceptance: waitd records a background report (ticket 01).

New state functions are imported inside each test so a missing one fails that
test alone."""
import json

from dispatcher.waitd import handle_ping

T, N = "portfolio_eval", 329
TASK = {"id": "b1", "type": "shell", "command": "make crap-gate"}


def _ping(d, **kw):
    handle_ping(json.dumps({"issue": N, "target": T, **kw}).encode(), d)


def _bg(d, target=T):
    from dispatcher.state import read_background
    return read_background(d, target, N)


def _place(d, tasks, now):
    from dispatcher.state import mark_background
    mark_background(d, T, N, tasks, now=now)


def test_background_report_leaves_background_marker_no_waiting(tmp_path):
    from dispatcher.state import has_waiting
    _ping(tmp_path, background_tasks=[TASK])
    assert _bg(tmp_path).tasks == ("b1",)
    assert not has_waiting(tmp_path, T, N)


def test_non_report_pings_leave_waiting_and_no_background(tmp_path):
    from dispatcher.state import clear_turn_markers, has_waiting
    for extra in ({}, {"background_tasks": []}, {"background_tasks": "x"},
                  {"background_tasks": {"id": "b1"}}):
        _ping(tmp_path, **extra)
        assert has_waiting(tmp_path, T, N), extra
        assert _bg(tmp_path) is None, extra
        clear_turn_markers(tmp_path, T, N)


def test_report_removes_waiting_and_waiting_removes_background(tmp_path):
    from dispatcher.state import has_waiting, mark_waiting
    mark_waiting(tmp_path, T, N)
    _ping(tmp_path, background_tasks=[TASK])
    assert not has_waiting(tmp_path, T, N)
    assert _bg(tmp_path) is not None
    _ping(tmp_path)
    assert has_waiting(tmp_path, T, N)
    assert _bg(tmp_path) is None


def test_empty_target_report_is_legacy_waiting_no_background(tmp_path):
    handle_ping(json.dumps({"issue": N, "target": "",
                            "background_tasks": [TASK]}).encode(), tmp_path)
    assert (tmp_path / f"waiting-{N}").exists()
    assert _bg(tmp_path) is None


def test_first_report_since_and_reported_are_now(tmp_path):
    _place(tmp_path, [TASK], 1000)
    bw = _bg(tmp_path)
    assert (bw.since, bw.reported) == (1000, 1000)


def test_same_identity_keeps_since(tmp_path):
    _place(tmp_path, [TASK], 1000)
    _place(tmp_path, [TASK], 2000)
    bw = _bg(tmp_path)
    assert (bw.since, bw.reported) == (1000, 2000)


def test_new_identity_restarts_since_and_replaces_tasks(tmp_path):
    _place(tmp_path, [TASK], 1000)
    _place(tmp_path, [{"id": "b2"}], 2000)
    bw = _bg(tmp_path)
    assert bw.since == 2000 and bw.tasks == ("b2",)


def test_idless_identity_is_the_whole_entry(tmp_path):
    a = {"type": "shell", "command": "make"}
    _place(tmp_path, [dict(a)], 1000)
    _place(tmp_path, [dict(a)], 2000)
    assert _bg(tmp_path).since == 1000
    _place(tmp_path, [{**a, "command": "make test"}], 3000)
    assert _bg(tmp_path).since == 3000


def test_invalid_json_marker_reads_absent(tmp_path):
    (tmp_path / f"background-{T}-{N}").write_text("{not json")
    assert _bg(tmp_path) is None


def test_clear_turn_markers_removes_both_markers(tmp_path):
    from dispatcher.state import clear_turn_markers, has_waiting, mark_waiting
    mark_waiting(tmp_path, T, N)
    _place(tmp_path, [TASK], 1000)
    clear_turn_markers(tmp_path, T, N)
    assert not has_waiting(tmp_path, T, N)
    assert _bg(tmp_path) is None


def test_marker_scoped_to_target(tmp_path):
    _ping(tmp_path, background_tasks=[TASK])
    assert _bg(tmp_path) is not None
    assert _bg(tmp_path, "other") is None
