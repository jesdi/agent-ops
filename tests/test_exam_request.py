"""Exam request and state read through the authenticated HTTP interface."""
import dataclasses
import fcntl
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources

CONSOLE = "https://box.tailnet.ts.net"
REQUEST = {"repo": "jesdi/general-skills", "pr": 240}
URL = CONSOLE + "/exam/jesdi/general-skills/240"
KICK = ("systemctl", "--user", "start", "--no-block", "agent-ops-dispatcher.service")


def build(tmp_path, monkeypatch, console_url=CONSOLE):
    calls = []

    def execute(argv, **kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("web.sources.subprocess.run", execute)
    cfg = dataclasses.replace(make_config(tmp_path / "state"), console_url=console_url)
    sources = Sources(cfg.state_dir, None, None, clock=lambda: 1000.0)
    return TestClient(create_app(cfg, sources)), calls, Path(cfg.state_dir)


@pytest.fixture
def console(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch)


def exam_dirs(state_dir):
    return sorted(p for p in (state_dir / "exams").glob("*/*/*") if p.is_dir())


def test_request_returns_url_new_and_pending(console):
    client, _, _ = console
    response = client.post("/api/exam", headers=HEADERS, json=REQUEST)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "new"
    assert body["pending"] is True
    assert body["url"] == URL


def test_repeating_a_request_returns_the_same_url_and_one_exam(console):
    client, _, state_dir = console
    first = client.post("/api/exam", headers=HEADERS, json=REQUEST)
    second = client.post("/api/exam", headers=HEADERS, json=REQUEST)
    assert first.status_code == second.status_code == 200
    assert second.json()["url"] == first.json()["url"] == URL
    assert len(exam_dirs(state_dir)) == 1


def test_each_request_kicks_the_dispatcher_once(console):
    client, calls, _ = console
    client.post("/api/exam", headers=HEADERS, json=REQUEST)
    assert calls == [KICK]
    client.post("/api/exam", headers=HEADERS, json=REQUEST)
    assert calls == [KICK, KICK]


def test_get_returns_the_state_without_exam_or_attempts(console):
    client, _, _ = console
    posted = client.post("/api/exam", headers=HEADERS, json=REQUEST).json()
    response = client.get("/api/exam/jesdi/general-skills/240", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == posted["state"] == "new"
    assert body["exam"] is None
    assert body["attempts"] == []


def test_get_unknown_pr_is_404(console):
    client, _, _ = console
    assert client.post("/api/exam", headers=HEADERS, json=REQUEST).status_code == 200
    assert client.get("/api/exam/jesdi/general-skills/999", headers=HEADERS).status_code == 404
    assert client.get("/api/exam/jesdi/general-skills/240", headers=HEADERS).status_code == 200


def test_exam_routes_need_the_tailscale_header(console):
    client, calls, state_dir = console
    assert client.post("/api/exam", headers=HEADERS, json=REQUEST).status_code == 200
    assert client.get("/api/exam/jesdi/general-skills/240", headers=HEADERS).status_code == 200
    assert client.post("/api/exam", json={"repo": "jesdi/other", "pr": 1}).status_code == 401
    assert client.get("/api/exam/jesdi/general-skills/240").status_code == 401
    assert calls == [KICK]
    assert len(exam_dirs(state_dir)) == 1


@pytest.mark.parametrize("body", [
    {"repo": "general-skills", "pr": 240},
    {"repo": "a/b/c", "pr": 240},
    {"repo": "jesdi/general-skills", "pr": 0},
    {**REQUEST, "head_sha": "a" * 40},
    {**REQUEST, "label": "exam:skip"},
    {**REQUEST, "mode": "skip"},
])
def test_invalid_request_is_422_and_creates_nothing(console, body):
    client, calls, state_dir = console
    assert client.post("/api/exam", headers=HEADERS, json=body).status_code == 422
    assert exam_dirs(state_dir) == []
    assert calls == []


def test_missing_console_url_is_503_naming_it(tmp_path, monkeypatch):
    client, _, state_dir = build(tmp_path, monkeypatch, console_url="")
    response = client.post("/api/exam", headers=HEADERS, json=REQUEST)
    assert response.status_code == 503
    assert "console_url" in response.text


def test_request_waits_for_the_exams_lock_before_writing_state(console):
    client, _, state_dir = console
    state_dir.mkdir(parents=True, exist_ok=True)
    done = threading.Event()
    with open(state_dir / "exams.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        worker = threading.Thread(
            target=lambda: (client.post("/api/exam", headers=HEADERS, json=REQUEST), done.set()))
        worker.start()
        assert not done.wait(0.5)
        assert exam_dirs(state_dir) == []
        fcntl.flock(held, fcntl.LOCK_UN)
    assert done.wait(10)
    worker.join()
    assert len(exam_dirs(state_dir)) == 1


def test_state_json_is_published_by_atomic_rename(console, monkeypatch):
    client, _, state_dir = console
    renames = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        renames.append((str(src), str(dst), json.loads(Path(src).read_text()),
                        Path(dst).exists()))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    assert client.post("/api/exam", headers=HEADERS, json=REQUEST).status_code == 200
    [(src, dst, written, existed)] = renames
    assert dst == str(state_dir / "exams/jesdi/general-skills/240/state.json")
    assert src != dst and not existed
    assert written["state"] == "new"
    assert json.loads(Path(dst).read_text()) == written
