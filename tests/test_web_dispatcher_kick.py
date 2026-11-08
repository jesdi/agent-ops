"""HTTP acceptance must not wait for a dispatcher oneshot pass to finish."""
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import time

from fastapi.testclient import TestClient

from dispatcher import state
from tests.test_web_sources import FakeGitHub, FakeSessions
from tests.webfakes import HEADERS, make_config, make_task
from web.app import create_app
from web.sources import Sources


def test_resume_acknowledges_persisted_intent_before_dispatcher_finishes(tmp_path, monkeypatch):
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    command = tmp_path / "systemctl"
    command.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        f"pathlib.Path({str(started)!r}).write_text(' '.join(sys.argv[1:]))\n"
        "if '--no-block' not in sys.argv:\n"
        f"    while not pathlib.Path({str(finished)!r}).exists(): time.sleep(0.01)\n"
    )
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    state.save(tmp_path, make_task())
    sources = Sources(str(tmp_path), FakeSessions(), FakeGitHub())
    with TestClient(create_app(make_config(tmp_path), sources)) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(client.post, "/api/task/alpha/7/resume",
                                   headers=HEADERS, json={"text": "continue"})
            try:
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert started.exists(), "dispatcher command did not start"
                intents = list((tmp_path / "intents").glob("*.json"))
                assert len(intents) == 1
                acknowledged = response.result(timeout=1)
                assert acknowledged.status_code == 202
                assert acknowledged.json()["intent"] == intents[0].name
                assert not finished.exists()
            finally:
                finished.touch()
                response.result(timeout=5)
