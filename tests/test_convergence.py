"""The updater and the dispatcher pass share this flock (ADR 0001 §3)."""
import fcntl

import pytest

from dispatcher.convergence import pass_lock


def _try_flock(path):
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fh.close()


def test_pass_lock_creates_state_dir_and_lock_file(tmp_path):
    state = tmp_path / "state"
    with pass_lock(str(state)):
        assert (state / "convergence.lock").exists()


def test_pass_lock_excludes_a_second_holder(tmp_path):
    with pass_lock(str(tmp_path)):
        with pytest.raises(BlockingIOError):
            _try_flock(tmp_path / "convergence.lock")


def test_pass_lock_releases_on_exit(tmp_path):
    with pass_lock(str(tmp_path)):
        pass
    _try_flock(tmp_path / "convergence.lock")  # must not raise
