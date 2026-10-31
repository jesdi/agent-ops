"""Acceptance tests for usage-socket ticket 02: the socket's file-system
boundary and safe start. Black-box: the real `python -m web` process (same
runner and config as ticket 01's locked tests), its exit status and stderr,
and the modes/ownership it leaves on disk."""
import grp
import httpx
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from test_usage_socket_acceptance import CONFIG, ROOT, RUNNER, _free_port


def _group_name() -> str:
    """A group we belong to, preferably not our primary one, so a socket left
    in the primary group cannot pass for 'the configured group'."""
    gids = [g for g in os.getgroups() if g != os.getgid()] or [os.getgid()]
    return grp.getgrgid(gids[0]).gr_name


@pytest.fixture
def work():
    w = Path(tempfile.mkdtemp(prefix="us-", dir="/tmp"))
    (w / "state").mkdir()
    yield w
    shutil.rmtree(w, ignore_errors=True)


def launch(work: Path, sock: Path, group: str, umask: int | None = None):
    (work / "targets.yaml").write_text(CONFIG.format(
        state=work / "state",
        extra=f"usage_socket: {sock}\nusage_socket_group: {group}\n"))
    (work / "runner.py").write_text(RUNNER)
    env = dict(os.environ, AGENT_OPS_WEB_PORT=str(_free_port()),
               AGENT_OPS_STATE_DIR=str(work / "state"), PYTHONPATH=str(ROOT))
    return subprocess.Popen(
        [sys.executable, str(work / "runner.py"), "--config", str(work / "targets.yaml")],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)


def wait_for_socket(proc, sock: Path, deadline=15.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        assert proc.poll() is None, f"console died: {proc.stderr.read()}"
        if sock.exists() and sock.is_socket():
            return
        time.sleep(0.05)
    raise AssertionError("no socket appeared")


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def refused(proc, deadline=15.0):
    try:
        code = proc.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        stop(proc)
        raise AssertionError("console kept running instead of refusing to start")
    err = proc.stderr.read()
    assert code != 0, "console exited 0"
    return err


def test_directory_0750_and_socket_0660_in_configured_group(work):
    sock, group = work / "run" / "usage.sock", _group_name()
    proc = launch(work, sock, group)
    try:
        wait_for_socket(proc, sock)
        d, s = sock.parent.stat(), sock.stat()
        gid = grp.getgrnam(group).gr_gid
        assert (stat.S_IMODE(d.st_mode), d.st_uid, d.st_gid) == (0o750, os.getuid(), gid)
        assert (stat.S_IMODE(s.st_mode), s.st_gid) == (0o660, gid)
    finally:
        stop(proc)


def test_never_more_permissive_than_0750_and_0660_under_umask_000(work):
    sock, group = work / "run" / "usage.sock", _group_name()
    seen, halt = [], threading.Event()

    def watch():
        while not halt.is_set():
            for p, cap in ((sock.parent, 0o750), (sock, 0o660)):
                try:
                    seen.append((str(p), stat.S_IMODE(os.lstat(p).st_mode), cap))
                except FileNotFoundError:
                    pass

    t = threading.Thread(target=watch)
    t.start()
    proc = launch(work, sock, group, umask=0)
    try:
        wait_for_socket(proc, sock)
        time.sleep(0.3)
    finally:
        halt.set()
        t.join()
        stop(proc)
    assert any(p == str(sock) for p, _, _ in seen), "never observed the socket"
    bad = [(p, oct(m)) for p, m, cap in seen if m & ~cap]
    assert not bad, f"too permissive while it existed: {bad[:3]}"


def test_user_outside_the_group_is_refused_by_the_os(work):
    sock, group = work / "run" / "usage.sock", _group_name()
    probe = ("import socket,sys\ns=socket.socket(socket.AF_UNIX)\n"
             "try: s.connect(sys.argv[1]); print('CONNECTED')\n"
             "except PermissionError: print('REFUSED')\n")
    proc = launch(work, sock, group)
    try:
        wait_for_socket(proc, sock)
        os.chmod(work, 0o755)
        r = subprocess.run(["sudo", "-n", "-u", "nobody", sys.executable, "-c", probe, str(sock)],
                           capture_output=True, text=True)
        if r.returncode != 0 and "REFUSED" not in r.stdout and "CONNECTED" not in r.stdout:
            pytest.skip("cannot act as a second local user here")
        assert r.stdout.strip() == "REFUSED"
    finally:
        stop(proc)


def _other_member():
    """(user, group) with user a member of a group we also belong to, user not
    us and not root (root bypasses permission bits), or None."""
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name
    for gid in os.getgroups():
        g = grp.getgrgid(gid)
        for u in g.gr_mem:
            if u not in (me, "root", "nobody"):
                return u, g.gr_name
    return None


def test_user_in_the_group_can_get_usage_through_the_socket(work):
    found = _other_member()
    if found is None:
        pytest.skip("no other local user shares a group with us")
    user, group = found
    sock = work / "run" / "usage.sock"
    probe = ("import sys,httpx\n"
             "with httpx.Client(transport=httpx.HTTPTransport(uds=sys.argv[1]),"
             " base_url='http://usage') as c: print('STATUS', c.get('/usage').status_code)\n")
    proc = launch(work, sock, group)
    try:
        wait_for_socket(proc, sock)
        # Group members must be able to traverse the directory and connect;
        # check this even on hosts where sudo cannot run the separate user.
        assert stat.S_IMODE(sock.parent.stat().st_mode) & 0o050 == 0o050, "configured group cannot traverse socket directory"
        assert stat.S_IMODE(sock.stat().st_mode) & 0o060 == 0o060, "configured group cannot connect to socket"
        os.chmod(work, 0o755)
        r = subprocess.run(["sudo", "-n", "-u", user, sys.executable, "-c", probe, str(sock)],
                           capture_output=True, text=True)
        if r.returncode != 0 and "STATUS" not in r.stdout and "Permission" not in r.stderr:
            pytest.skip("cannot act as a second local user here")
        assert r.stdout.strip() == "STATUS 200", r.stderr
    finally:
        stop(proc)


def test_leftover_socket_from_a_killed_run_is_replaced(work):
    sock, group = work / "run" / "usage.sock", _group_name()
    first = launch(work, sock, group)
    wait_for_socket(first, sock)
    first.send_signal(signal.SIGKILL)
    first.wait(timeout=10)
    assert sock.is_socket(), "precondition: the killed run left its socket"
    second = launch(work, sock, group)
    try:
        end = time.monotonic() + 15
        while True:
            assert second.poll() is None, f"console died: {second.stderr.read()}"
            try:
                with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)),
                                  base_url="http://usage") as c:
                    r = c.get("/usage")
                break
            except httpx.TransportError:
                assert time.monotonic() < end, "leftover socket never answered"
                time.sleep(0.1)
        assert r.status_code == 200
        assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o750
        assert stat.S_IMODE(sock.stat().st_mode) == 0o660
    finally:
        stop(second)


def test_regular_file_at_the_path_is_refused_and_kept(work):
    run = work / "run"; run.mkdir()
    sock = run / "usage.sock"
    sock.write_text("keep me")
    err = refused(launch(work, sock, _group_name()))
    assert str(sock) in err
    assert sock.read_text() == "keep me" and not sock.is_symlink()


def test_directory_at_the_path_is_refused_and_kept(work):
    run = work / "run"; run.mkdir()
    sock = run / "usage.sock"
    sock.mkdir()
    (sock / "inner.txt").write_text("keep me")
    err = refused(launch(work, sock, _group_name()))
    assert str(sock) in err
    assert sock.is_dir() and not sock.is_symlink()
    assert [p.name for p in sock.iterdir()] == ["inner.txt"]
    assert (sock / "inner.txt").read_text() == "keep me"


def test_symlink_at_the_path_is_refused_and_nothing_changes(work):
    run = work / "run"; run.mkdir()
    target = work / "target.txt"
    target.write_text("target body")
    sock = run / "usage.sock"
    sock.symlink_to(target)
    err = refused(launch(work, sock, _group_name()))
    assert str(sock) in err
    assert sock.is_symlink() and os.readlink(sock) == str(target)
    assert target.read_text() == "target body"


def test_unknown_group_is_refused_and_no_socket_is_created(work):
    sock = work / "run" / "usage.sock"
    err = refused(launch(work, sock, "trakcer"))
    assert "trakcer" in err
    assert not [p for p in work.rglob("*") if p.is_socket()]
