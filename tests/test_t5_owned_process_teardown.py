"""Generic owned process regression; no supervisor or product listener runs."""

import json
import errno
import os
import signal
import sys
import time

import pytest

from t5_executable_support import ExecutableSession


LEADER = r'''
import json, os, pathlib, signal, subprocess, sys, time
control, writing = map(pathlib.Path, sys.argv[1:])
child_code = r"""
import json, os, pathlib, signal, sys, time
control, writing = map(pathlib.Path, sys.argv[1:])
def terminating(signum, frame):
    (control / 'child-term').write_text('TERM received')
signal.signal(signal.SIGTERM, terminating)
(control / 'child.json').write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}))
while not (control / 'release-child').exists():
    writing.mkdir(parents=True, exist_ok=True)
    (writing / 'last-write').write_text('generic owned writer')
    time.sleep(0.002)
"""
signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(0))
subprocess.Popen([sys.executable, '-B', '-c', child_code, str(control), str(writing)])
(control / 'leader-ready').write_text('ready')
while True:
    signal.pause()
'''


def exists(identifier, *, group=False):
    try:
        (os.killpg if group else os.kill)(identifier, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Unknown existence must not authorize directory removal.
    return True


def wait_gone(process, *, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        process.poll()
        if not exists(process.pid, group=True):
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("redundant_permission_error", [False, True], ids=["delayed-writer", "redundant-kill-eperm"])
def test_exit_waits_for_owned_descendant_writers_after_leader_exits(tmp_path, monkeypatch, redundant_permission_error):
    session = ExecutableSession()
    control = tmp_path / "owned-control"
    control.mkdir()
    process = session.spawn([sys.executable, "-B", "-c", LEADER, str(control),
                             str(session.directory / "delayed-writer")], "generic-leader")
    cleanup = session.temporary.cleanup
    original_killpg = os.killpg
    signals = []
    def signal_group(pgid, signum):
        original_killpg(pgid, signum)
        if pgid == process.pid and signum != 0:
            signals.append(signum)
        if pgid == process.pid and signum == signal.SIGKILL and redundant_permission_error:
            raise PermissionError(errno.EPERM, "generic redundant owned-group KILL race")
    monkeypatch.setattr(os, "killpg", signal_group)
    try:
        assert session.until(lambda: (control / "child.json").exists() and
                             (control / "leader-ready").exists()), "generic owned child readiness barrier"
        child = json.loads((control / "child.json").read_text())
        assert child["pgid"] == process.pid and child["pid"] != process.pid
        assert exists(child["pid"]), "regression needs a live owned writer"

        def strict_cleanup():
            assert not exists(child["pid"]), "parent exited while owned descendant writer remained alive before strict directory cleanup"
            assert not exists(process.pid, group=True), "owned process group must disappear before strict cleanup"
            cleanup()

        session.temporary.cleanup = strict_cleanup
        session.__exit__(None, None, None)
        assert process.poll() is not None and not exists(child["pid"])
        assert (control / "child-term").exists(), "normal group TERM must precede bounded escalation"
        assert signals == [signal.SIGTERM, signal.SIGKILL], "only the resistant owned group requires bounded KILL escalation"
        assert not session.directory.exists(), "verified writers must permit immediate strict directory removal"
    finally:
        # Independent safety cleanup also works with the intentionally unsafe old
        # fixture. Only this exact separately spawned owned group is selected.
        if exists(process.pid, group=True):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                assert wait_gone(process), "unverified permission error for owned regression group"
        process.wait(timeout=5)
        assert wait_gone(process), "owned regression group remains after safety cleanup"
        for stream in session.logs:
            stream.close()
        if session.directory.exists():
            cleanup()
