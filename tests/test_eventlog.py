import json
from pathlib import Path

from dispatcher import eventlog


def test_append_writes_one_json_line_with_all_fields(tmp_path):
    eventlog.append_event(tmp_path, "claimed", target="portfolio_eval",
                          issue=42, stage="queued", model="", detail="")
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["event"] == "claimed"
    assert d["target"] == "portfolio_eval"
    assert d["issue"] == 42
    assert d["stage"] == "queued"
    assert d["model"] == ""
    assert d["actor"] == "dispatcher"
    assert d["detail"] == ""
    assert d["ts"].endswith("+00:00") or d["ts"].endswith("Z")  # UTC iso8601


def test_append_creates_state_dir_if_missing(tmp_path):
    sd = tmp_path / "nested" / "state"
    eventlog.append_event(sd, "failed", issue=7, actor="operator",
                          detail="killed by operator")
    d = json.loads((sd / "events.jsonl").read_text())
    assert d["actor"] == "operator" and d["detail"] == "killed by operator"


def test_read_tail_returns_oldest_first_and_honours_limit(tmp_path):
    for i in range(5):
        eventlog.append_event(tmp_path, "stage-started", issue=i)
    tail = eventlog.read_tail(tmp_path, limit=3)
    assert [e["issue"] for e in tail] == [2, 3, 4]


def test_read_tail_skips_malformed_lines(tmp_path):
    eventlog.append_event(tmp_path, "claimed", issue=1)
    with (tmp_path / "events.jsonl").open("a") as fh:
        fh.write("{not json\n")
        fh.write("[1, 2, 3]\n")   # valid JSON but not an object
    eventlog.append_event(tmp_path, "parked", issue=2)
    tail = eventlog.read_tail(tmp_path)
    assert [e["event"] for e in tail] == ["claimed", "parked"]


def test_read_tail_empty_when_no_file(tmp_path):
    assert eventlog.read_tail(tmp_path) == []


def test_rotation_archives_the_full_file_before_append(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("x" * (eventlog.MAX_BYTES + 1) + "\n")
    eventlog.append_event(tmp_path, "resumed", issue=9)
    archives = sorted(tmp_path.glob("events-*.jsonl"))
    assert len(archives) == 1, "oversized file renamed to events-<stamp>.jsonl"
    assert archives[0].stat().st_size > eventlog.MAX_BYTES  # retained, never deleted
    fresh = eventlog.read_tail(tmp_path)
    assert [e["event"] for e in fresh] == ["resumed"]  # new file starts clean


def test_append_never_raises_into_the_caller(tmp_path, capsys):
    # A file where the DIRECTORY should be forces mkdir/open to fail.
    blocker = tmp_path / "state"
    blocker.write_text("i am a file, not a directory")
    eventlog.append_event(blocker / "sub", "claimed", issue=1)  # must not raise
    assert "event log append failed" in capsys.readouterr().err
