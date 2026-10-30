"""Projects in the console: the project list and each card's project."""
from dataclasses import replace

from fastapi.testclient import TestClient

from dispatcher.state import Stage
from tests.configfakes import make_project
from tests.webfakes import FakeSources, HEADERS, make_config, make_repository, make_task
from web.app import create_app

CORE = make_project("core")
SIDE = make_project("side", paused=True)


def _rig(tmp_path):
    """Project core holds alpha and beta; project side (paused) holds gamma."""
    fake = FakeSources()
    cfg = make_config(tmp_path, repositories=[
        replace(make_repository("alpha"), project=CORE),
        replace(make_repository("beta", "jesdi/beta"), project=CORE),
        replace(make_repository("gamma", "jesdi/gamma"), project=SIDE)])
    return fake, TestClient(create_app(cfg, fake))


def test_projects_lists_every_project_in_config_order_even_without_tasks(tmp_path):
    _fake, client = _rig(tmp_path)
    assert client.get("/api/projects", headers=HEADERS).json() == {"projects": [
        {"name": "core", "paused": False, "repositories": ["alpha", "beta"]},
        {"name": "side", "paused": True, "repositories": ["gamma"]}]}


def _cards(body):
    return {(c["repository"], c["issue"]): c["project"]
            for col in body["columns"] for c in col["cards"]}


def test_board_cards_carry_their_project_and_a_removed_repository_has_none(tmp_path):
    fake, client = _rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, repository="beta"),
                       make_task(issue=8, repository="gamma", stage=Stage.DONE),
                       make_task(issue=9, repository="gone", stage=Stage.DONE)]
    expected = {("beta", 7): "core", ("gamma", 8): "side", ("gone", 9): ""}
    assert _cards(client.get("/api/board", headers=HEADERS).json()) == expected
    assert _cards(client.get("/api/board/snapshot", headers=HEADERS).json()) == expected


def test_ghosts_carry_the_project_whose_queue_ranked_them(tmp_path):
    fake, client = _rig(tmp_path)
    ready = {"status": "Ready", "blocked": False, "labels": ["auto"],
             "title": "t", "url": "u", "boost": 0, "score": 1.0}
    fake.rank = {"beta": ([{"number": 3, **ready}], "2026-07-25T00:00:00+00:00", False),
                 "gamma": ([{"number": 4, **ready}], "2026-07-25T00:00:00+00:00", False)}
    body = client.get("/api/board", headers=HEADERS).json()
    queued = next(c for c in body["columns"] if c["key"] == "queued")
    assert [(g["repository"], g["project"]) for g in queued["ghosts"]] == [
        ("beta", "core"), ("gamma", "side")]


def test_task_detail_card_carries_its_project(tmp_path):
    fake, client = _rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, repository="gamma")]
    body = client.get("/api/task/gamma/7", headers=HEADERS).json()
    assert body["card"]["project"] == "side"
