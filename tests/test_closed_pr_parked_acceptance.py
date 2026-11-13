"""A closed PR ends a parked task failed, not parked (#316).

Seam: main.run_pass with the shared fakes, and GET /api/board for the column.
"""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import dispatcher.main as main
from dispatcher import intents, messages
from dispatcher.state import PARK_HUMAN, WHY_LOOP_CAP, Stage, load
from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, cfg, deps,
                             make_task, patch_teardown, patch_usage, payload,
                             pr_open_task)
from tests.webfakes import HEADERS
from web.app import create_app
from web.sources import Sources

PARKED = dict(park=PARK_HUMAN, park_msg_id=55, park_reason=WHY_LOOP_CAP,
              park_note="ci loop hit its cap")


def _column(c, issue):
    with TestClient(create_app(c, Sources(c.state_dir, sessions=None,
                                          github=None))) as client:
        board = client.get("/api/board", headers=HEADERS).json()
    return next(col["key"] for col in board["columns"]
                for card in col["cards"] if card["issue"] == issue)


def _reply(c, run, text="please retry"):
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                         {"text": text}, actor="jesdi@github", epoch_ms=1)
    run()
    run()


def _setup(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, pr_number=88, **PARKED)
    gh = FakeGitHub()
    gh.pr_payloads[88] = payload(state="CLOSED")
    notif, sess = FakeNotifier(), FakeSessions(alive=(42,))
    return c, lambda: main.run_pass(c, deps(gh, sess, notifier=notif)), notif, sess


def test_closed_pr_ends_a_parked_task_failed_and_in_the_failed_column(
        tmp_path, monkeypatch):
    c, run, notif, sess = _setup(tmp_path, monkeypatch)
    run()
    run()
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.park) == (Stage.FAILED, "")
    assert 42 in sess.ended
    assert notif.sent.count("pr_closed") == 1
    assert _column(c, 42) == "failed"


def test_a_reply_after_the_close_is_queued_and_wakes_nothing(
        tmp_path, monkeypatch):
    c, run, _, _ = _setup(tmp_path, monkeypatch)
    run()
    _reply(c, run)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.park) == (Stage.FAILED, "")
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [
        "please retry"]


def test_a_task_saved_failed_and_parked_by_the_old_code_is_not_woken(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.FAILED, pr_number=88,
              updated_at=datetime.now(timezone.utc).isoformat(), **PARKED)
    _reply(c, lambda: main.run_pass(c, deps()))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.park) == (Stage.FAILED, "")
    assert _column(c, 42) == "failed"
