"""The one atomic file writer of the dispatcher's state and settings files:
dispatcher.state.write_atomic / write_json_atomic."""
import json
import multiprocessing
import os
import threading

import pytest

from dispatcher import state
from dispatcher.state import write_atomic, write_json_atomic

MIB = 1 << 20


def names(d):
    return sorted(p.name for p in d.iterdir())


@pytest.fixture
def d(tmp_path):
    # conftest keeps other dirs in tmp_path: give the writer a directory of its own
    d = tmp_path / "d"
    d.mkdir()
    return d


@pytest.fixture
def f(d):
    f = d / "f"
    f.write_text("old")
    return f


def test_a_text_payload_creates_missing_parent_directories(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    write_atomic(root / "a" / "b.json", '{"k": 1}')
    assert (root / "a" / "b.json").read_bytes() == b'{"k": 1}'
    assert names(root / "a") == ["b.json"]


def test_a_bytes_payload_is_written_as_is(d):
    write_atomic(d / "blob.bin", b"\x00\xff\n")
    assert (d / "blob.bin").read_bytes() == b"\x00\xff\n"


def test_a_dotted_file_name_keeps_its_name(d):
    write_atomic(d / "owner-repo.name-149.json", "x")
    assert names(d) == ["owner-repo.name-149.json"]
    assert (d / "owner-repo.name-149.json").read_text() == "x"


def test_the_file_is_0644_under_a_strict_umask(d):
    old = os.umask(0o077)
    try:
        write_atomic(d / "f", "x")
    finally:
        os.umask(old)
    assert (d / "f").stat().st_mode & 0o777 == 0o644


def test_replacing_a_0600_file_leaves_it_0644(d):
    (d / "f").write_text("old")
    (d / "f").chmod(0o600)
    write_atomic(d / "f", "x")
    assert (d / "f").stat().st_mode & 0o777 == 0o644


def _boom(*a, **k):
    raise OSError("disk full")


def test_a_failed_rename_keeps_the_old_content_and_no_temp_file(d, f, monkeypatch):
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        write_atomic(f, "new")
    monkeypatch.undo()
    assert f.read_text() == "old"
    assert names(d) == ["f"]


def test_a_failed_write_keeps_the_old_content_and_no_temp_file(d, f, monkeypatch):
    real = os.fdopen

    class Failing:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def write(self, data):
            raise OSError("disk full")

        def close(self):
            self.fh.close()

    monkeypatch.setattr(os, "fdopen", lambda *a, **k: Failing(real(*a, **k)))
    with pytest.raises(OSError):
        write_atomic(f, "new")
    monkeypatch.undo()
    assert f.read_text() == "old"
    assert names(d) == ["f"]


def test_a_failed_chmod_keeps_the_old_content_and_no_temp_file(d, f, monkeypatch):
    monkeypatch.setattr(os, "chmod", _boom)
    monkeypatch.setattr(os, "fchmod", _boom)
    with pytest.raises(OSError):
        write_atomic(f, "new")
    monkeypatch.undo()
    assert f.read_text() == "old"
    assert names(d) == ["f"]


def test_threads_writing_one_path_never_tear_it(d):
    path = d / "shared"
    payloads = [bytes([65 + i]) * MIB for i in range(8)]
    seen, torn, done = [], [], threading.Event()
    errors = []

    def writer(payload):
        try:
            for _ in range(50):
                write_atomic(path, payload)
        except BaseException as e:
            errors.append(e)

    def reader():
        while not done.is_set():
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            seen.append(1)
            if data not in payloads:
                torn.append(len(data))

    r = threading.Thread(target=reader)
    ws = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    r.start()
    for w in ws:
        w.start()
    for w in ws:
        w.join()
    done.set()
    r.join()
    assert errors == []
    assert torn == []
    assert path.read_bytes() in payloads
    assert names(d) == ["shared"]


def _hammer(path, payload, others):
    bad = 0
    for _ in range(50):
        write_atomic(path, payload)
        if path.read_bytes() not in others:
            bad += 1
    return bad


def test_two_processes_writing_one_path_never_tear_it(d):
    path = d / "shared"
    payloads = [b"a" * MIB, b"b" * MIB]
    with multiprocessing.get_context("fork").Pool(2) as pool:
        bad = pool.starmap(_hammer, [(path, p, payloads) for p in payloads])
    assert bad == [0, 0]
    assert path.read_bytes() in payloads
    assert names(d) == ["shared"]


def test_json_wrapper_default_is_compact_in_insertion_order(d):
    write_json_atomic(d / "p", {"b": 1, "a": [1, 2]})
    assert (d / "p").read_bytes() == b'{"b": 1, "a": [1, 2]}'
    # the keyword defaults are part of the interface
    write_json_atomic(d / "q", {"b": 1, "a": [1, 2]}, indent=None, sort_keys=False)
    assert (d / "q").read_bytes() == b'{"b": 1, "a": [1, 2]}'


def test_json_wrapper_takes_indent_and_sort_keys(d):
    doc = {"b": 1, "a": 2}
    write_json_atomic(d / "p", doc, indent=2, sort_keys=True)
    assert (d / "p").read_text() == json.dumps(doc, indent=2, sort_keys=True)
