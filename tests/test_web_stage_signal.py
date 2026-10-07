"""Ticket 06: the web read of .agent/stage.json is bounded and never raises;
the progress text only shows for a working implement signal."""
import json
import os
import threading

import pytest

from dispatcher.state import STAGE_SIGNAL_MAX_BYTES, StageSignal
from tests.webfakes import make_config, make_task
from web.read_model import PROGRESS_MAX_CHARS, _implement_progress
from web.sources import Sources


def _read(tmp_path, wt):
    return Sources(make_config(tmp_path), None, None).stage_signal(str(wt))


def _wt(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".agent").mkdir(parents=True)
    return wt, wt / ".agent" / "stage.json"


def _prog(note, status="working", stage="implement"):
    return _implement_progress(
        make_task(), StageSignal(stage=stage, status=status, note=note))


@pytest.mark.parametrize("status", [
    "blocked", "done", "awaiting-answers", "awaiting-review", "awaiting-ci"])
def test_only_a_working_signal_is_progress(status):
    assert _prog("a reason", status=status) is None


def test_working_signal_is_progress():
    assert _prog("2/4 tickets merged") == "2/4 tickets merged"


def test_long_note_is_cut_with_a_sign():
    got = _prog("  " + "x" * 10_000 + "  ")
    assert got == "x" * (PROGRESS_MAX_CHARS - 1) + "…"
    assert _prog("x" * PROGRESS_MAX_CHARS) == "x" * PROGRESS_MAX_CHARS


def test_reads_a_good_file(tmp_path):
    wt, f = _wt(tmp_path)
    f.write_text(json.dumps({"stage": "implement", "status": "working",
                             "note": "hi"}))
    assert _read(tmp_path, wt) == StageSignal("implement", "working", "hi")


@pytest.mark.parametrize("raw", [
    "{not json", "[]", "null", '{"status": "working"}',
    '{"stage": "implement", "status": "working", "note": 3}'])
def test_unusable_content(tmp_path, raw):
    wt, f = _wt(tmp_path)
    f.write_text(raw)
    got = _read(tmp_path, wt)
    assert got is None or got.note == ""


def test_deeply_nested_json_is_none(tmp_path):
    wt, f = _wt(tmp_path)
    f.write_text("[" * 100_000)
    assert _read(tmp_path, wt) is None


def test_fifo_does_not_hang(tmp_path):
    wt, f = _wt(tmp_path)
    os.mkfifo(f)
    out = []
    th = threading.Thread(target=lambda: out.append(_read(tmp_path, wt)),
                          daemon=True)
    th.start()
    th.join(1)
    assert not th.is_alive() and out == [None]


def test_symlink_is_not_followed(tmp_path):
    wt, f = _wt(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text('{"stage": "implement", "status": "working"}')
    f.symlink_to(outside)
    assert _read(tmp_path, wt) is None


def test_oversized_file_is_none(tmp_path):
    wt, f = _wt(tmp_path)
    f.write_text('{"stage": "implement", "status": "working", "note": "'
                 + "x" * STAGE_SIGNAL_MAX_BYTES + '"}')
    assert _read(tmp_path, wt) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_file_is_none(tmp_path):
    wt, f = _wt(tmp_path)
    f.write_text('{"stage": "implement", "status": "working"}')
    f.chmod(0)
    assert _read(tmp_path, wt) is None


def test_missing_file_is_none(tmp_path):
    assert _read(tmp_path, tmp_path / "nowhere") is None
