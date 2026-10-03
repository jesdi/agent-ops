"""The priority mode file as two processes share it: the console writes it,
the dispatcher (maybe another user) reads it. dispatcher.priority.load/save."""
import json
import os
from datetime import datetime, timezone

import pytest

from dispatcher import priority

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ROUTED = {"anthropic", "openai"}


def test_save_creates_a_missing_state_dir(tmp_path):
    d = tmp_path / "not" / "yet"
    priority.save(d, "openai", actor="op", now=NOW)
    assert priority.load(d, ROUTED) == "openai"


def test_saved_file_is_world_readable_whatever_the_umask(tmp_path):
    old = os.umask(0o077)
    try:
        priority.save(tmp_path, "openai", actor="op", now=NOW)
    finally:
        os.umask(old)
    assert (tmp_path / priority.FILE).stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("broken", ["dump", "replace"])
def test_a_failed_save_leaves_no_temp_file_and_the_old_mode(
        tmp_path, monkeypatch, broken):
    tmp_path = tmp_path / "state"  # conftest keeps a home dir in tmp_path
    priority.save(tmp_path, "anthropic", actor="op", now=NOW)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(*{"dump": (json, "dump"), "replace": (os, "replace")}[broken],
                        boom)
    with pytest.raises(OSError):
        priority.save(tmp_path, "openai", actor="op", now=NOW)
    monkeypatch.undo()
    assert [p.name for p in tmp_path.iterdir()] == [priority.FILE]
    assert priority.load(tmp_path, ROUTED) == "anthropic"


def test_two_saves_do_not_share_a_temp_file(tmp_path, monkeypatch):
    temps = []
    real = os.replace
    monkeypatch.setattr(os, "replace",
                        lambda src, dst: (temps.append(str(src)), real(src, dst)))
    priority.save(tmp_path, "openai", actor="a", now=NOW)
    priority.save(tmp_path, "anthropic", actor="b", now=NOW)
    assert len(set(temps)) == 2


def test_load_reads_auto_for_json_nested_past_the_recursion_limit(tmp_path):
    (tmp_path / priority.FILE).write_text('{"mode": ' + "[" * 100_000)
    assert priority.load(tmp_path, ROUTED) == "auto"
