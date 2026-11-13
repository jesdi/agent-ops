"""The messages and artifact writes go through the one atomic writer: same bytes
as before, mode 0644 whatever the umask, no temp file, and no fixed temp name
left anywhere in dispatcher/."""
import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

from dispatcher import messages, task_artifacts

ROOT = Path(__file__).resolve().parents[1]
MSG_DIR = "messages"


@contextmanager
def umask(mask):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def mode(p):
    return p.stat().st_mode & 0o777


def line(id, text, created_at, delivered_at=None):
    return json.dumps({"id": id, "text": text, "actor": "op",
                       "created_at": created_at, "delivered_at": delivered_at}) + "\n"


def seed(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def test_mark_delivered_rewrites_the_jsonl_at_0644_and_leaves_no_temp(tmp_path):
    p = tmp_path / MSG_DIR / "t-42.jsonl"
    first = line("a", "one", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")
    seed(p, first + line("b", "two", "2026-01-01T00:00:01+00:00"))
    with umask(0o077):
        messages.mark_delivered(tmp_path, "t", 42, ["b"])
    raw = p.read_bytes().decode()
    one, two = raw.splitlines(keepends=True)
    stamp = json.loads(two)["delivered_at"]
    assert stamp
    assert one == first
    assert two == line("b", "two", "2026-01-01T00:00:01+00:00", stamp)
    assert mode(p) == 0o644
    assert sorted(os.listdir(p.parent)) == ["t-42.jsonl"]


def test_migrate_legacy_merges_into_the_keyed_file_at_0644(tmp_path, monkeypatch):
    d = tmp_path / MSG_DIR
    dest, legacy = d / "t-42.jsonl", d / "42.jsonl"
    keep = line("x", "kept", "2026-01-02T00:00:00+00:00")
    old = line("y", "older", "2026-01-01T00:00:00+00:00")
    seed(dest, keep)
    seed(legacy, old + keep)           # "x" is already in dest and is skipped
    legacy_alive_at_replace = []
    real_replace = os.replace

    def replace(src, dst, *a, **kw):
        legacy_alive_at_replace.append(legacy.exists())
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", replace)
    with umask(0o077):
        messages.migrate_legacy(tmp_path, ["t"])
    assert dest.read_bytes().decode() == old + keep
    assert not legacy.exists()
    assert legacy_alive_at_replace == [True]
    assert mode(dest) == 0o644
    assert sorted(os.listdir(d)) == ["t-42.jsonl"]


def test_copy_content_writes_the_raw_bytes_at_0644_and_leaves_no_temp(tmp_path):
    dest = tmp_path / "content" / "report.bin"
    with umask(0o077):
        task_artifacts._copy_content(dest, b"\x00raw")
    assert dest.read_bytes() == b"\x00raw"
    assert mode(dest) == 0o644
    assert sorted(os.listdir(dest.parent)) == ["report.bin"]


def test_an_unchanged_index_is_not_rewritten(tmp_path, monkeypatch):
    path = tmp_path / "index.json"
    value = {"b": [1, 2], "a": "x"}
    with umask(0o077):
        task_artifacts._write(path, value)
    assert mode(path) == 0o644          # the first write already went through the atomic writer
    os.utime(path, (1_000_000_000, 1_000_000_000))
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *a, **kw: replaced.append(a) or real_replace(*a, **kw))
    task_artifacts._write(path, {"a": "x", "b": [1, 2]})
    assert replaced == []
    assert path.stat().st_mtime == 1_000_000_000


def test_a_changed_index_is_rewritten_sorted_at_0644_and_leaves_no_temp(tmp_path):
    path = tmp_path / "index.json"
    task_artifacts._write(path, {"a": 1})
    with umask(0o077):
        task_artifacts._write(path, {"a": 2})
    assert path.read_bytes().decode() == json.dumps({"a": 2}, indent=2, sort_keys=True)
    assert mode(path) == 0o644
    assert sorted(os.listdir(tmp_path)) == ["index.json"]


def test_no_fixed_temp_name_is_left_in_dispatcher():
    out = subprocess.run(
        ["git", "grep", "-nE", r'with_suffix\("\.tmp"\)|\+ "\.tmp"|\.tmp"\)', "--", "dispatcher/*.py"],
        cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
    assert len(out) == 1, out
    assert re.match(r"dispatcher/state\.py:\d+:.*mkstemp", out[0])
