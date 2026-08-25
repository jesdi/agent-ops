"""Per-issue operator message queue: append, drain, stamp, survive corruption."""
import json
from pathlib import Path

from dispatcher import messages


def test_append_then_read_round_trip(tmp_path):
    m = messages.append(tmp_path, 42, "use oauth", "jesdi@github")
    assert m.text == "use oauth"
    assert m.actor == "jesdi@github"
    assert m.delivered_at == ""
    assert m.id != ""
    assert m.created_at != ""
    assert [x.id for x in messages.all_messages(tmp_path, 42)] == [m.id]


def test_appends_keep_order_and_are_per_issue(tmp_path):
    messages.append(tmp_path, 42, "first", "op")
    messages.append(tmp_path, 42, "second", "op")
    messages.append(tmp_path, 99, "other issue", "op")
    assert [m.text for m in messages.all_messages(tmp_path, 42)] == [
        "first", "second"]
    assert [m.text for m in messages.all_messages(tmp_path, 99)] == [
        "other issue"]


def test_undelivered_excludes_stamped_and_mark_delivered_is_selective(tmp_path):
    a = messages.append(tmp_path, 42, "first", "op")
    b = messages.append(tmp_path, 42, "second", "op")
    messages.mark_delivered(tmp_path, 42, [a.id])
    assert [m.text for m in messages.undelivered(tmp_path, 42)] == ["second"]
    stamped = {m.id: m.delivered_at for m in messages.all_messages(tmp_path, 42)}
    assert stamped[a.id] != ""
    assert stamped[b.id] == ""


def test_mark_delivered_is_idempotent_and_ignores_unknown_ids(tmp_path):
    a = messages.append(tmp_path, 42, "first", "op")
    messages.mark_delivered(tmp_path, 42, [a.id])
    first_stamp = messages.all_messages(tmp_path, 42)[0].delivered_at
    messages.mark_delivered(tmp_path, 42, [a.id, "no-such-id"])
    assert messages.all_messages(tmp_path, 42)[0].delivered_at == first_stamp
    assert messages.undelivered(tmp_path, 42) == []


def test_missing_file_reads_as_empty(tmp_path):
    assert messages.all_messages(tmp_path, 7) == []
    assert messages.undelivered(tmp_path, 7) == []
    messages.mark_delivered(tmp_path, 7, ["x"])  # must not raise


def test_malformed_line_is_skipped_with_a_warning(tmp_path, capsys):
    good = messages.append(tmp_path, 42, "keep me", "op")
    p = Path(tmp_path) / messages.MESSAGES_DIR / "42.jsonl"
    with p.open("a") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps({"text": "no id"}) + "\n")
    kept = messages.all_messages(tmp_path, 42)
    assert [m.id for m in kept] == [good.id]
    assert "[warn]" in capsys.readouterr().err


def test_mark_delivered_drops_malformed_lines_it_rewrites(tmp_path):
    a = messages.append(tmp_path, 42, "first", "op")
    p = Path(tmp_path) / messages.MESSAGES_DIR / "42.jsonl"
    with p.open("a") as fh:
        fh.write("{not json\n")
    messages.mark_delivered(tmp_path, 42, [a.id])
    assert [m.text for m in messages.all_messages(tmp_path, 42)] == ["first"]


def test_undelivered_counts_across_issues(tmp_path):
    a = messages.append(tmp_path, 42, "one", "op")
    messages.append(tmp_path, 42, "two", "op")
    messages.append(tmp_path, 99, "three", "op")
    messages.mark_delivered(tmp_path, 42, [a.id])
    assert messages.undelivered_counts(tmp_path) == {42: 1, 99: 1}


def test_undelivered_counts_no_dir_is_empty(tmp_path):
    assert messages.undelivered_counts(tmp_path) == {}


def test_undelivered_counts_ignores_non_issue_filenames(tmp_path):
    messages.append(tmp_path, 42, "one", "op")
    (Path(tmp_path) / messages.MESSAGES_DIR / "notes.jsonl").write_text("{}\n")
    assert messages.undelivered_counts(tmp_path) == {42: 1}
