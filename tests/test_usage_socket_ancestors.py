"""The usage socket's directory cannot be swapped through a writable ancestor."""
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from web.__main__ import _bind_usage_socket


@pytest.fixture
def w():  # short: AF_UNIX paths are limited to ~104 bytes
    d = Path(tempfile.mkdtemp(dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_missing_ancestors_are_not_writable_by_others_under_umask_000(w):
    old = os.umask(0)
    try:
        _bind_usage_socket(str(w / "a" / "b" / "run" / "u.sock"), "").close()
    finally:
        os.umask(old)
    for d in (w / "a", w / "a" / "b"):
        assert stat.S_IMODE(os.lstat(d).st_mode) == 0o755
    assert stat.S_IMODE(os.lstat(w / "a" / "b" / "run").st_mode) == 0o750


def test_world_writable_ancestor_is_refused_and_no_socket_created(w):
    os.chmod(w, 0o777)
    with pytest.raises(SystemExit, match=str(w)):
        _bind_usage_socket(str(w / "run" / "u.sock"), "")
    assert not (w / "run" / "u.sock").exists()


def test_symlink_in_a_world_writable_directory_is_refused(w):
    (w / "open").mkdir()
    os.chmod(w / "open", 0o777)
    (w / "safe").mkdir()
    (w / "open" / "link").symlink_to(w / "safe")
    with pytest.raises(SystemExit, match="open"):
        _bind_usage_socket(str(w / "open" / "link" / "run" / "u.sock"), "")


def test_symlink_hop_in_a_world_writable_directory_is_refused(w):
    for d in ("open", "safe", "real"):
        (w / d).mkdir()
    os.chmod(w / "open", 0o777)
    (w / "open" / "hop").symlink_to(w / "real")
    (w / "safe" / "link").symlink_to(w / "open" / "hop")
    with pytest.raises(SystemExit, match="open"):
        _bind_usage_socket(str(w / "safe" / "link" / "run" / "u.sock"), "")


def test_symlinked_safe_chain_is_accepted(w):
    (w / "real").mkdir()
    (w / "link").symlink_to("real")
    _bind_usage_socket(str(w / "link" / ".." / "link" / "run" / "u.sock"), "").close()
    assert (w / "real" / "run" / "u.sock").is_socket()


def test_dotdot_after_a_symlink_checks_where_the_kernel_lands(w):
    # safe/link/../run resolves to open/run, not safe/run.
    (w / "safe").mkdir()
    (w / "open" / "sub").mkdir(parents=True)
    os.chmod(w / "open", 0o777)
    (w / "safe" / "link").symlink_to(w / "open" / "sub")
    with pytest.raises(SystemExit, match="open"):
        _bind_usage_socket(str(w / "safe" / "link" / ".." / "run" / "u.sock"), "")
    assert not (w / "open" / "run" / "u.sock").exists()


def test_symlink_owned_by_another_user_is_refused(w, monkeypatch):
    # In a sticky directory (/tmp) the link's owner can still replace it.
    (w / "real").mkdir()
    (w / "hop").symlink_to(w / "real")
    hop, real_lstat = w.resolve() / "hop", os.lstat

    def lstat(p, **kw):
        st = real_lstat(p, **kw)
        if isinstance(p, (str, Path)) and Path(p) == hop:
            st = os.stat_result((*st[:4], os.getuid() + 1, *st[5:]))
        return st

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(SystemExit, match="hop"):
        _bind_usage_socket(str(w / "hop" / "run" / "u.sock"), "")
    assert not (w / "real" / "run" / "u.sock").exists()
