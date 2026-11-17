"""A closed PR ends a parked task failed, not parked (#316).

Seam: main.run_pass with the shared fakes, and GET /api/board for the column.
"""
import json
from datetime import datetime, timezone

import pytest

from fastapi.testclient import TestClient

import dispatcher.main as main
from dispatcher import intents, messages
from dispatcher.state import Stage, _path, load
from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, cfg, deps,
                             make_task, patch_teardown, patch_usage, payload,
                             pr_open_task)
from tests.webfakes import HEADERS
from web.app import create_app
from web.sources import Sources
from tests.statuses import failed, human

PARKED = dict(status=human("loop-cap", "ci loop hit its cap"), park_msg_id=55)


def _card(c, issue):
    with TestClient(create_app(c, Sources(c.state_dir, sessions=None,
                                          github=None))) as client:
        board = client.get("/api/board", headers=HEADERS).json()
    return next((col["key"], card) for col in board["columns"]
                for card in col["cards"] if card["issue"] == issue)


def _column(c, issue):
    return _card(c, issue)[0]


def _failed_card_asks_nothing(c, issue):
    """The failed column, and no question left over from the park (#132)."""
    column, card = _card(c, issue)
    assert (column, card["park"], card["park_note"], card["park_reason"]) == (
        "failed", "", "", "")


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
    assert (t.stage, t.status) == (Stage.PR_OPEN, failed("pr-closed"))
    on_disk = json.loads(_path(c.state_dir, "portfolio_eval", 42).read_text())
    assert on_disk["status"] == {"kind": "failed", "detail": {"reason": "pr-closed"}}
    assert on_disk["park_msg_id"] == 0
    assert 42 in sess.ended
    assert notif.sent.count("pr_closed") == 1
    _failed_card_asks_nothing(c, 42)


def test_a_reply_after_the_close_is_queued_and_wakes_nothing(
        tmp_path, monkeypatch):
    c, run, _, _ = _setup(tmp_path, monkeypatch)
    run()
    _reply(c, run)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status) == (Stage.PR_OPEN, failed("pr-closed"))
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [
        "please retry"]


@pytest.mark.parametrize("reason", ["pr-closed", "unknown"])
def test_a_reply_to_a_failed_task_wakes_nothing(tmp_path, monkeypatch, reason):
    """A failed task is never parked: a reply leaves it failed, never
    Resuming (#316; a file the old code saved failed and parked is
    migrated to failed)."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PR_OPEN, status=failed(reason), pr_number=88,
              updated_at=datetime.now(timezone.utc).isoformat())
    _reply(c, lambda: main.run_pass(c, deps()))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status) == (Stage.PR_OPEN, failed(reason))
    _failed_card_asks_nothing(c, 42)
