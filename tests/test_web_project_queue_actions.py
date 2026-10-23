"""Project-tab queue actions through HTTP and the real Sources write path."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from dispatcher.config import Project, ProjectRepository
from tests.test_web_qualified_task_api import FakeGitHub, FakeSessions, FRONT, BACK, row
from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources


class ProjectGitHub(FakeGitHub):
    unavailable = False

    def rank_rows(self, project):
        if self.unavailable:
            raise ConnectionError("GitHub unavailable")
        return self.rows[project.key]

    def set_boost(self, target, issue, amount, **kw):
        self.writes.append((target, issue, kw["repo"]))

    def set_status(self, target, issue, status, **kw):
        self.writes.append((target, issue, kw["repo"]))


@pytest.fixture
def project_queue(tmp_path):
    repositories = [ProjectRepository(r, f"/clones/{r}", f"/wt/{r}", "rank", "gate",
                                      boost_field_id=r) for r in (FRONT, BACK)]
    project = Project("shop", "Shop", "example", 7, "F", "R", "I", repositories)
    cfg = replace(make_config(tmp_path), targets=[], projects=[project])
    gh = ProjectGitHub()
    gh.rows["shop"] = [row(r) for r in (FRONT, BACK)]
    sources = Sources(str(tmp_path), FakeSessions(), gh, systemctl=("true",))
    return gh, TestClient(create_app(cfg, sources))


@pytest.mark.parametrize("action,payload", [
    ("boost", {"amount": 3}), ("next", {"force": True}), ("ready", {})])
def test_project_board_queue_actions_route_to_member_repository(project_queue, action, payload):
    gh, client = project_queue
    board = client.get("/api/board", headers=HEADERS)
    assert board.status_code == 200
    assert BACK in board.text
    if action == "ready":
        gh.rows["shop"] = [row(r, status="Backlog") for r in (FRONT, BACK)]
    response = client.post(f"/api/queue/{action}", headers=HEADERS,
                           json={"issue": 12, "repo": BACK, **payload})
    assert response.status_code == 200, response.text
    assert gh.writes
    for target, issue, repo in gh.writes:
        assert (target.name, target.repo, issue, repo) == ("shop", BACK, 12, BACK)
        assert target.boost_field_id == BACK


def test_project_queue_ambiguous_number_refuses_write(project_queue):
    gh, client = project_queue
    response = client.post("/api/queue/boost", headers=HEADERS,
                           json={"issue": 12, "amount": 1})
    assert response.status_code == 409
    assert FRONT in response.text and BACK in response.text
    assert gh.writes == []


@pytest.mark.parametrize("warm_cache", [False, True])
def test_project_queue_refuses_stale_writes(project_queue, warm_cache):
    gh, client = project_queue
    if warm_cache:
        assert client.get("/api/board", headers=HEADERS).status_code == 200
    gh.unavailable = True
    response = client.post("/api/queue/boost", headers=HEADERS,
                           json={"issue": 12, "repo": BACK, "amount": 1})
    assert response.status_code == 409
    assert "queue data is stale" in response.text
    assert gh.writes == []


def test_project_queue_write_reads_fresh_board(project_queue):
    gh, client = project_queue
    assert client.get("/api/board", headers=HEADERS).status_code == 200
    gh.rows["shop"] = []
    response = client.post("/api/queue/boost", headers=HEADERS,
                           json={"issue": 12, "repo": BACK, "amount": 1})
    assert response.status_code == 404
    assert gh.writes == []
