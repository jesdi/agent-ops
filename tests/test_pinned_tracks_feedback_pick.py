"""Acceptance tests for ticket 04 of pinned-tracks: PR feedback has one pick,
`picks["feedback"]`, chosen from the task track's implement list at the first
feedback session and reused for every later round. Black-box through
run_pass (launches from the sessions fake, state from dispatcher.state) and
the state file round trip. Policy, mode and usage come from
tests/test_pinned_tracks_order.py (standard implement = sonnet, sol;
architecture is pinned: astra, fable)."""
import json

import pytest

import dispatcher.main as main
from dispatcher import state
from dispatcher.state import NO_SLOT, PARK_WAKE, Stage, load
from tests.test_main import (FakeGitHub, FakeSessions, cfg, deps, make_task,
                             patch_usage, patch_workspace)
from tests.test_pinned_tracks_order import (ASTRA, FABLE, SOL, SONNET, ahead,
                                            deny, launched, make_cfg)

BOTH = ["openai", "anthropic"]
FABLE_PICK = f"anthropic/{FABLE}@medium"
SOL_PICK = f"{SOL}@medium"
SONNET_PICK = f"anthropic/{SONNET}@medium"


def saved(c, issue=42):
    return load(c.state_dir, "portfolio_eval", issue)


def feedback_round(c, issue=42, **kw):
    """A task at pr-open with PR feedback queued; one pass."""
    make_task(c, issue=issue, stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=12,
              feedback_pending=True, **kw)
    sess = FakeSessions()
    main.run_pass(c, deps(FakeGitHub(), sess))
    return sess


def test_architecture_feedback_takes_the_written_first_entry(
        tmp_path, monkeypatch):
    # Mode anthropic would put fable first; the track is pinned.
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="anthropic")
    sess = feedback_round(c, track="architecture", picks={},
                          implement_providers=BOTH,
                          ticket_tracks={2: "frontend"}, ticket_cursor=2,
                          ticket_count=2)
    assert launched(sess) == [(ASTRA, "medium")]
    assert saved(c).picks["feedback"] == f"{ASTRA}@medium"


def test_standard_feedback_in_mode_openai_runs_on_the_openai_entry(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    sess = feedback_round(c, track="standard", picks={},
                          implement_providers=["anthropic"])
    assert launched(sess) == [(SOL, "medium")]
    assert saved(c).picks["feedback"] == SOL_PICK


def test_first_feedback_round_falls_back_when_the_first_entry_is_denied(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, deny(ahead(), "gpt-astra"),
                 mode="anthropic")
    sess = feedback_round(c, track="architecture",
                          picks={}, implement_providers=BOTH)
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]
    assert saved(c).picks == {"feedback": FABLE_PICK}


def test_second_round_reuses_the_feedback_pick(tmp_path, monkeypatch):
    # Everything admitted, astra is first in written order, yet the pick holds.
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="anthropic")
    sess = feedback_round(c, track="architecture",
                          picks={"feedback": FABLE_PICK},
                          implement_providers=BOTH)
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]
    assert saved(c).picks["feedback"] == FABLE_PICK


def test_feedback_waits_when_its_pick_is_denied(tmp_path, monkeypatch):
    # Astra is admitted, but the pick is fable: nothing launches.
    c = make_cfg(tmp_path, monkeypatch, deny(ahead(), "claude-fable-5-1"),
                 mode="anthropic")
    sess = feedback_round(c, track="architecture",
                          picks={"feedback": FABLE_PICK},
                          implement_providers=BOTH)
    assert sess.spawned == []
    got = saved(c)
    assert got.stage is Stage.PR_OPEN and got.feedback_pending is True
    assert got.picks["feedback"] == FABLE_PICK


def test_leftover_implement_pick_at_pr_open_becomes_the_feedback_pick(
        tmp_path, monkeypatch):
    # A task past implement carries no implement pick in the finished system:
    # a leftover one is from before the change, and its pick moves (design).
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    sess = feedback_round(c, track="standard", picks={"implement": SONNET_PICK},
                          implement_providers=["anthropic"])
    assert launched(sess) == [(f"anthropic/{SONNET}", "medium")]
    got = saved(c)
    assert got.picks == {"feedback": SONNET_PICK}
    assert got.implement_providers == ["anthropic"]


def test_feedback_adds_no_implement_pick_to_a_task_without_one(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    sess = feedback_round(c, track="standard", picks={})
    assert launched(sess) == [(SOL, "medium")]
    assert saved(c).picks == {"feedback": SOL_PICK}


def old_shape(c, pick, **raw_over):
    """A pr-open state file as before this change: the implement pick only."""
    make_task(c, issue=42, stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=12,
              feedback_pending=True, track="standard", picks={"implement": pick})
    p = state._path(c.state_dir, "portfolio_eval", 42)
    raw = json.loads(p.read_text())
    raw.pop("implement_providers", None)
    raw.update(raw_over)
    p.write_text(json.dumps(raw))
    assert "feedback" not in raw["picks"]


def test_old_shape_state_next_feedback_round_stays_on_its_provider(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="anthropic")
    old_shape(c, SOL_PICK)
    sess = FakeSessions()
    main.run_pass(c, deps(FakeGitHub(), sess))
    assert launched(sess) == [(SOL, "medium")]
    assert saved(c).picks["feedback"] == SOL_PICK


def test_old_shape_state_read_moves_the_pick_to_the_feedback_key(tmp_path):
    c = cfg(tmp_path)
    old_shape(c, SOL_PICK)
    got = saved(c)
    assert got.picks == {"feedback": SOL_PICK}
    assert got.implement_providers == ["openai"]


@pytest.mark.parametrize("implement", [
    {"implement": "anthropic/claude-opus-5"}, {}], ids=["anthropic", "absent"])
def test_crashed_address_review_is_reported_with_the_feedback_runtime(
        tmp_path, monkeypatch, implement):
    # One state dir per case: a failure is filed once per fingerprint.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    gh = FakeGitHub()
    wt = make_task(c, issue=42, stage=Stage.ADDRESS_REVIEW, pr_number=12,
                   picks={**implement, "feedback": "openai/gpt-5-codex@high"})
    main.run_pass(c, deps(gh, FakeSessions(alive=set())))
    body = gh.created_issues[0][2]
    assert (f"- repro: `cd {wt} && codex resume --last  # inside "
            "session image`") in body


def test_resumed_address_review_keeps_the_feedback_key(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="anthropic")
    make_task(c, issue=42, stage=Stage.ADDRESS_REVIEW, pr_number=12,
              track="architecture", park=PARK_WAKE,
              picks={"feedback": FABLE_PICK}, implement_providers=BOTH)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(FakeGitHub(), sess))
    assert [r[2] for r in sess.resumed] == [f"anthropic/{FABLE}"]
    assert saved(c).picks == {"feedback": FABLE_PICK}
