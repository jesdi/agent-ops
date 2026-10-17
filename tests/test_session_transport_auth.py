"""Regression coverage for the socket trust boundary and owned wake teardown."""
import json
from io import BytesIO
import socket
import struct
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dispatcher import main, state, waitd
from tests.test_main import (ADMIT_ALL, FakeSessions, cfg, deps, make_task,
                             patch_usage)


@pytest.mark.parametrize("peer_pid,accepted", [(101, True), (202, False)])
def test_legacy_transport_checks_the_senders_container(tmp_path, monkeypatch,
                                                       peer_pid, accepted):
    # Both containers share a socket and UID; only the frontend's host PID
    # can authenticate a tokenless ping naming the legacy frontend execution.
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    connection = Mock()
    connection.getsockopt.return_value = struct.pack("3i", peer_pid, 1000, 1000)
    top = Mock(return_value=SimpleNamespace(stdout="HPID\n 101\n"))
    monkeypatch.setattr(waitd.subprocess, "run", top)
    c = cfg(tmp_path)
    make_task(c, repo="example/frontend")
    task = state.load(c.state_dir, "portfolio_eval", 42, repo="example/frontend")
    state.record_session_owner(c.state_dir, task.target, task.issue, task.repo)
    state.issue_execution_token(c.state_dir, task.target, 13, "example/backend")
    body = json.dumps({"target": task.target, "issue": task.issue,
                       "session_id": "conversation"}).encode()
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body))}, rfile=BytesIO(body),
        server=SimpleNamespace(state_dir=c.state_dir), connection=connection,
        send_response=Mock(), end_headers=Mock())
    waitd._Handler.do_POST(handler)
    assert state.has_waiting(c.state_dir, task.target, task.issue,
                             repo=task.repo) is accepted
    assert (state.read_session(c.state_dir, task.target, task.issue,
                               repo=task.repo) is not None) is accepted
    assert top.call_args.args[0] == ["podman", "top", "task-portfolio_eval-42", "hpid"]


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("podman", 5),
                                      subprocess.CalledProcessError(1, "podman")])
def test_legacy_transport_fails_closed(monkeypatch, failure):
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    connection = Mock()
    connection.getsockopt.return_value = struct.pack("3i", 101, 1000, 1000)
    monkeypatch.setattr(waitd.subprocess, "run", Mock(side_effect=failure))
    assert not waitd._legacy_peer_matches(connection, "shop", 12)


@pytest.mark.parametrize("ends_successfully", [True, False])
@pytest.mark.parametrize("legacy", [True, False])
def test_qualified_login_wake_ends_before_launch(tmp_path, monkeypatch,
                                                ends_successfully, legacy):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, park=state.PARK_WAKE, repo=c.targets[0].repo)
    task = state.load(c.state_dir, "portfolio_eval", 42, repo=c.targets[0].repo)
    if legacy:
        state.record_session_owner(c.state_dir, task.target, task.issue, task.repo)
    else:
        state.issue_execution_token(c.state_dir, task.target, task.issue, task.repo)
    state.write_session(c.state_dir, task.target, task.issue,
                        state.SessionRecord("recorded", "implement"), repo=task.repo)
    sess = FakeSessions(alive=[task.issue])
    original_end = sess.end

    def end(target, issue, repo=""):
        original_end(target, issue, repo)
        if ends_successfully:
            sess.alive_set.discard(issue)

    monkeypatch.setattr(sess, "end", end)
    main._resume_woken(c, deps(sess=sess), admit=ADMIT_ALL, order=tuple)
    assert sess.ended
    assert bool(sess.resumed) is ends_successfully
