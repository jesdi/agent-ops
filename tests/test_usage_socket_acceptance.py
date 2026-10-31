"""Acceptance tests for usage-socket ticket 01: the console serves its usage
view on a configured Unix socket. Black-box: the real `python -m web` process
(a runner that stubs Sources.usage and freezes the clock, then calls
web.__main__.main), HTTP over the Unix socket, HTTP over TCP for comparison.

Interface defined here: targets.yaml keys `usage_socket` (path) and
`usage_socket_group` (group name; defaults to the current user's group)."""
import grp
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
OP = {"Tailscale-User-Login": "operator@example.com"}

RUNNER = '''
import sys, time
from datetime import datetime, timedelta, timezone
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW

import web.app, web.read_model
web.app.datetime = Frozen
web.read_model.datetime = Frozen

from dispatcher.usage import ProviderUsage, Window, WindowKind
from web.sources import Sources

def usage(self, routed=()):
    wk = lambda used: Window(WindowKind.WEEKLY, None, used, NOW + timedelta(days=3.5))
    return {"anthropic": ProviderUsage("anthropic", "oauth", NOW.timestamp(), (wk(0.62),)),
            "openai": ProviderUsage("openai", "oauth", NOW.timestamp(), (wk(0.20),))}
Sources.usage = usage

from web.__main__ import main
main(sys.argv[1:])
'''

CONFIG = """state_dir: {state}
models:
  triage: [claude-sonnet-5]
  untracked: standard
  tracks:
    standard:
      when: Everything.
      spec: [claude-sonnet-5, openai/gpt-luna]
      plan: [claude-sonnet-5, openai/gpt-luna]
      implement: [claude-sonnet-5, openai/gpt-luna]
      review: [claude-sonnet-5, openai/gpt-luna]
{extra}"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Console:
    def __init__(self, work: Path, sock: Path | None, group: str | None = None):
        self.work, self.sock, self.port = work, sock, _free_port()
        self.state = work / "state"
        self.state.mkdir()
        extra = ""
        if sock is not None:
            extra += f"usage_socket: {sock}\n"
            extra += f"usage_socket_group: {group or grp.getgrgid(os.getgid()).gr_name}\n"
        (work / "targets.yaml").write_text(
            CONFIG.format(state=self.state, extra=extra))
        (work / "runner.py").write_text(RUNNER)
        env = dict(os.environ, AGENT_OPS_WEB_PORT=str(self.port),
                   AGENT_OPS_STATE_DIR=str(self.state), PYTHONPATH=str(ROOT))
        self.proc = subprocess.Popen(
            [sys.executable, str(work / "runner.py"), "--config",
             str(work / "targets.yaml")],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)

    def wait_up(self, deadline=15.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"console died during startup: {self.proc.stderr.read()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("console never listened on TCP")
        return self

    def await_socket(self, deadline=10.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end and not self.sock.exists():
            time.sleep(0.05)
        assert self.sock.exists(), "no socket file appeared at the configured path"

    def uds(self, method, path, **kw):
        self.await_socket()
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(self.sock)),
                          base_url="http://usage") as c:
            return c.request(method, path, **kw)

    def tcp(self, method, path, **kw):
        headers = {**OP, **kw.pop("headers", {})}
        return httpx.request(method, f"http://127.0.0.1:{self.port}{path}",
                             headers=headers, **kw)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture
def console():
    # AF_UNIX paths are short on macOS: keep the work dir directly under /tmp.
    work = Path(tempfile.mkdtemp(prefix="us-", dir="/tmp"))
    c = Console(work, work / "run" / "usage.sock")
    try:
        yield c.wait_up()
    finally:
        c.stop()
        shutil.rmtree(work, ignore_errors=True)


def test_socket_body_equals_api_usage_body(console):
    assert console.tcp("POST", "/api/priority", json={"mode": "openai"}).status_code == 200
    via_socket = console.uds("GET", "/usage")
    via_tcp = console.tcp("GET", "/api/usage")
    assert via_socket.status_code == 200 and via_tcp.status_code == 200
    body = via_socket.json()
    assert body["priority"]["mode"] == "openai"
    assert body["priority"]["first"] == "openai"
    assert via_socket.content == via_tcp.content


def test_priority_change_shows_on_next_socket_read(console):
    assert console.uds("GET", "/usage").json()["priority"]["mode"] == "auto"
    assert console.tcp("POST", "/api/priority", json={"mode": "anthropic"}).status_code == 200
    assert console.uds("GET", "/usage").json()["priority"]["mode"] == "anthropic"


def test_socket_serves_only_get_usage(console):
    before = console.tcp("GET", "/api/usage").json()["priority"]["mode"]
    assert console.uds("POST", "/api/priority", json={"mode": "openai"}).status_code == 404
    assert console.uds("POST", "/usage").status_code == 405
    assert console.uds("GET", "/api/board").status_code == 404
    assert console.tcp("GET", "/api/usage").json()["priority"]["mode"] == before


def test_socket_needs_no_header_and_a_forged_one_grants_nothing(console):
    assert console.uds("GET", "/usage").status_code == 200
    assert console.uds("GET", "/api/health", headers=OP).status_code == 404


def test_no_socket_configured_creates_none_and_tcp_unchanged(console):
    # control: the same console with a path configured does create its socket
    console.await_socket()
    work = Path(tempfile.mkdtemp(prefix="us-", dir="/tmp"))
    c = Console(work, None)
    try:
        c.wait_up()
        assert c.tcp("GET", "/api/usage").status_code == 200
        assert not [p for p in work.rglob("*") if p.is_socket()]
    finally:
        c.stop()
        shutil.rmtree(work, ignore_errors=True)
