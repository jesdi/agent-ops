"""Ticket 04 fixer: reads and Telegram writes that must not alias two
repositories' issues of one number."""
from dataclasses import replace as dc_replace

import dispatcher.main as main
from dispatcher import state
from telegram.inbound import Command, classify
from tests.test_main import FakeGitHub, cfg, deps, patch_events, patch_usage, row
from tests.webfakes import make_task
from web.sources import Sources

FRONT, BACK = "example/shop-frontend", "example/shop-backend"


class LiveSessions:
    def is_alive(self, target, issue, repo=""):
        return repo == BACK

    def capture_tail(self, target, issue, lines=25, repo=""):
        return "LIVE"

    def capture_history(self, target, issue, lines=2000, repo=""):
        return "LIVE"


def sources(tmp_path, *repos):
    for repo in repos:
        state.save(tmp_path, make_task(issue=12, target="shop", repo=repo))
    return Sources(str(tmp_path), LiveSessions(), None, systemctl=("true",))


def test_colliding_tasks_do_not_share_a_live_session_or_snapshot(tmp_path):
    src = sources(tmp_path, FRONT, BACK)
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "task-shop-12.txt").write_text("SHARED")
    assert src.pane_tail("shop", 12, FRONT) == ""
    assert src.pane_history("shop", 12, 10, FRONT) == ""
    assert src._read_snapshot("shop", 12, FRONT) == ""
    assert src._read_snapshot("shop", 12, BACK) == ""
    assert src.pane_tail("shop", 12, BACK) == "LIVE"


def test_the_sole_task_reads_its_session_and_the_writers_snapshot(tmp_path):
    src = sources(tmp_path, BACK)
    assert src.pane_tail("shop", 12, BACK) == "LIVE"
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / f"task-{state.task_key('shop', 12, BACK)}.txt").write_text("LAST")
    assert src._read_snapshot("shop", 12, BACK) == "LAST"


def test_old_reference_reaches_the_one_archived_qualified_task(tmp_path):
    done = make_task(issue=12, target="shop", repo=FRONT, stage=state.Stage.DONE)
    state.archive(tmp_path, done)
    src = Sources(str(tmp_path), LiveSessions(), None, systemctl=("true",))
    assert src.task("shop", 12).repo == FRONT


def test_boost_and_next_parse_a_qualified_reference():
    assert classify(f"/boost {BACK}#12 2", 0) == Command(
        name="boost", issue=12, repo=BACK, amount=2)
    assert classify(f"/next {BACK}#12 force", 0) == Command(
        name="next", issue=12, repo=BACK, force=True)


def test_qualified_boost_writes_to_the_selected_repository(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    gh = FakeGitHub(rows=[row(12) | {"repo": FRONT}, row(12) | {"repo": BACK}])
    gh.set_boost = lambda target, issue, value, **kw: gh.boosts.append((issue, value, kw))
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=12, amount=1)])
    main.run_pass(dc_replace(c, targets=[c.targets[0]]), d)
    assert gh.boosts == []   # a bare number names two repositories
    patch_events(monkeypatch, [Command(name="boost", issue=12, repo=BACK, amount=1)])
    main.run_pass(dc_replace(c, targets=[c.targets[0]]), d)
    assert [kw for _, _, kw in gh.boosts] == [{"repo": BACK}]


def test_old_cancel_of_a_colliding_backlog_number_asks_for_the_repository(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_web_qualified_task_api import rig
    gh, client = rig(tmp_path, tasks=False)
    from tests.webfakes import HEADERS
    assert client.post("/api/task/shop/12/cancel", headers=HEADERS).status_code == 409
    assert client.post("/api/task/shop/12/reply", json={"text": "x"},
                       headers=HEADERS).status_code == 409
