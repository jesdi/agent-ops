"""#87 acceptance: an unreadable quarantine record blocks its issue and says so.

A record the dispatcher cannot read keeps its issue unclaimed, so the console
must list it, explicitly marked unreadable, and let the operator retry it —
and the dispatcher must keep blocking it instead of raising mid-pass.
"""
import json

import pytest
from fastapi.testclient import TestClient

from dispatcher import failures
from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources

UNREADABLE = {73: b'{"blocker_issue": \xff\xfe}',  # torn write: invalid UTF-8
              74: b"null", 75: b"[1, 2]", 76: b"{not json"}


class _NullGitHub:
    def rank_rows(self, _): return []

    def issue_state(self, _repo, _issue): return "OPEN"


class _NullSessions:
    def is_alive(self, _): return False


def _state(tmp_path):
    d = tmp_path / "quarantine"
    d.mkdir(parents=True)
    for issue, raw in UNREADABLE.items():
        (d / f"alpha-{issue}.json").write_bytes(raw)
    failures.write_quarantine(tmp_path, "alpha", 77, "r", 5, "fp")
    cfg = make_config(tmp_path)
    return TestClient(create_app(cfg, Sources(cfg.state_dir, _NullSessions(),
                                              _NullGitHub())))


def test_failures_lists_unreadable_records_as_unreadable(tmp_path):
    client = _state(tmp_path)
    resp = client.get("/api/failures", headers=HEADERS)
    assert resp.status_code == 200
    rows = {q["task_issue"]: q for q in resp.json()["quarantined"]}
    assert sorted(rows) == [73, 74, 75, 76, 77]
    for issue in UNREADABLE:
        assert rows[issue]["repository"] == "alpha"
        assert rows[issue]["readable"] is False
        assert rows[issue]["blocker_open"] is None
    assert rows[77]["readable"] is True


def test_retry_route_accepts_an_unreadable_record(tmp_path):
    client = _state(tmp_path)
    resp = client.post("/api/task/alpha/74/retry", headers=HEADERS)
    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize("issue", sorted(UNREADABLE))
def test_dispatcher_keeps_blocking_an_unreadable_record(tmp_path, issue):
    _state(tmp_path)
    assert failures.check_quarantine(tmp_path, _NullGitHub(), "alpha", issue) is True
