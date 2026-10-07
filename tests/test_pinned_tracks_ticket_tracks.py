"""Acceptance tests for ticket 06 of pinned-tracks: a ticket can name a pinned
track for the implement stage (`Track: <name>` line). Black-box through
run_pass: ticket files in the task's worktree, plan and implement done signals,
launches read from the sessions fake, state from dispatcher.state.

Assumed observables (design.md): `TaskState.ticket_tracks` maps ticket number to
track name (keys compared as ints); `picks["implement"]` exists only while a
ticket is in progress; a ticket set that is refused resumes the plan session
with the reason (sess.resumed[0][1]); a ticket that cannot start because its
track is not pinned parks the task for the operator (park == PARK_HUMAN, reason
in park_note). Policy, mode and usage come from tests/test_pinned_tracks_order.py."""
import json
import re
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher import execution_overrides, state
from dispatcher.state import PARK_HUMAN, PARK_WAKE, Stage, load
from tests.test_main import (GOOD_TICKET, FakeSessions, deps, make_task,
                             valid_spec, write_tickets)
from tests.test_pinned_tracks_order import (ASTRA, FABLE, OPUS, SONNET,
                                            _track, ahead, deny, enter,
                                            launched, make_cfg, policy,
                                            policy_raw)
from dispatcher.models import parse_policy

PLAN_DONE = {"stage": "plan", "status": "done", "note": "tickets",
             "artifact": ".agent/tickets"}
IMPL_DONE = {"stage": "implement", "status": "done"}


def saved(c, issue=42):
    return load(c.state_dir, "portfolio_eval", issue)


def tracks_of(c):
    return {int(k): v for k, v in saved(c).ticket_tracks.items()}


def write_ticket(wt, n, *tracks):
    d = wt / ".agent" / "tickets"
    d.mkdir(parents=True, exist_ok=True)
    extra = "".join(f"\nTrack: {t}\n" for t in tracks)
    (d / f"{n:02d}-t{n}.md").write_text(
        GOOD_TICKET.replace("**Blocked by:** None",
                            "**Blocked by:** None\n" + extra))


def setup(tmp_path, monkeypatch, task_track, tickets, usages=None, **kw):
    """A task in plan whose plan session wrote `tickets`: one tuple of Track
    names (maybe empty) per ticket."""
    c = make_cfg(tmp_path, monkeypatch, usages or ahead(), **kw)
    wt = make_task(c, issue=42, stage=Stage.PLAN, track=task_track)
    for n, tracks in enumerate(tickets, 1):
        write_ticket(wt, n, *tracks)
    return c, wt


def usage_now(monkeypatch, usages):
    monkeypatch.setattr(main, "fetch_all", lambda cfg, **k: usages)


def step(c, wt, signal, alive=True):
    (wt / ".agent" / "stage.json").write_text(json.dumps(signal))
    sess = FakeSessions(alive={42} if alive else set())
    main.run_pass(c, deps(sess=sess))
    return sess


def run_all(c, wt, n):
    """Plan done, then implement done after each ticket but the last pass;
    returns every launch (model, effort) in order."""
    out = []
    for sig in [PLAN_DONE] + [IMPL_DONE] * (n - 1):
        out += launched(step(c, wt, sig))
    return out


def spec_done_plan_prompt(tmp_path, monkeypatch, track):
    raw = policy_raw()
    raw["tracks"]["trivial"] = _track(
        [f"{SONNET}@medium"], [f"{SONNET}@medium"], [f"{SONNET}@medium"],
        [f"{SONNET}@medium"])
    c = make_cfg(tmp_path, monkeypatch, ahead(), models=parse_policy(raw))
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, track=track)
    valid_spec(wt)
    sess = step(c, wt, {"stage": "spec", "status": "done",
                        "artifact": "spec.md", "track": track})
    plans = [s for s in sess.spawned if s[1] == "plan"]
    assert len(plans) == 1
    return plans[0][3]


# --- plan prompt ---------------------------------------------------------

@pytest.mark.parametrize("track", ["standard", "frontend", "architecture"])
def test_plan_prompt_offers_the_track_line_with_the_usable_names(
        tmp_path, monkeypatch, track):
    prompt = spec_done_plan_prompt(tmp_path, monkeypatch, track)
    assert "Track:" in prompt
    para = next(p for p in prompt.split("\n\n") if "Track:" in p)
    assert "architecture" in para and "frontend" in para
    assert "trivial" not in prompt


def test_plan_prompt_of_a_security_task_allows_no_ticket_track(
        tmp_path, monkeypatch):
    prompt = spec_done_plan_prompt(tmp_path, monkeypatch, "security")
    assert re.search(r"(?i)no ticket", prompt)
    assert "frontend" not in prompt and "architecture" not in prompt


# --- routing ---------------------------------------------------------------

def test_a_ticket_track_chooses_the_list_for_that_ticket_only(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture",
                  [(), ("frontend",), ()], mode="openai")
    assert run_all(c, wt, 3) == [
        (ASTRA, "medium"), (f"anthropic/{FABLE}", "medium"), (ASTRA, "medium")]


def test_the_accepted_ticket_set_is_copied_into_the_task_state(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture",
                  [(), ("frontend",), ()], mode="openai")
    step(c, wt, PLAN_DONE)
    assert tracks_of(c) == {2: "frontend"}


def test_a_ticket_track_on_an_unpinned_task(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "standard", [("architecture",), ()])
    assert run_all(c, wt, 2) == [
        (ASTRA, "medium"), (f"anthropic/{SONNET}", "medium")]


def test_review_ignores_ticket_tracks_and_avoids_the_implement_provider(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "standard",
                  [("architecture",), ("architecture",)], mode="openai")
    out = run_all(c, wt, 2)
    out += launched(step(c, wt, IMPL_DONE))
    assert out == [(ASTRA, "medium")] * 2 + [(f"anthropic/{OPUS}", "medium")]


def test_two_models_of_one_provider_count_as_one_provider_for_review(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture",
                  [("frontend",), ("frontend",)])
    out = launched(step(c, wt, PLAN_DONE))
    usage_now(monkeypatch, deny(ahead(), FABLE))
    out += launched(step(c, wt, IMPL_DONE))
    usage_now(monkeypatch, ahead())
    out += launched(step(c, wt, IMPL_DONE))
    assert out == [(f"anthropic/{FABLE}", "medium"),
                   (f"anthropic/{OPUS}", "medium"), (ASTRA, "high")]
    assert saved(c).implement_providers == ["anthropic"]


def test_a_security_task_without_ticket_tracks_runs_both_on_its_list(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "security", [(), ()])
    assert run_all(c, wt, 2) == [(ASTRA, "high")] * 2


# --- refusal ---------------------------------------------------------------

REFUSED = [
    ("architecture", [(), ("standard",)], 2, "standard"),
    ("standard", [("backend",), ()], 1, "backend"),
    ("standard", [("security",), ()], 1, "security"),
    ("standard", [("frontend", "frontend"), ()], 1, "Track"),
    ("security", [(), ("frontend",)], 2, "Track"),
]


@pytest.mark.parametrize("task_track,tickets,number,word", REFUSED,
                         ids=["standard-track", "unknown-track", "security",
                              "two-lines", "security-task"])
def test_an_invalid_ticket_track_sends_the_plan_back(
        tmp_path, monkeypatch, task_track, tickets, number, word):
    c, wt = setup(tmp_path, monkeypatch, task_track, tickets)
    sess = step(c, wt, PLAN_DONE)
    assert sess.spawned == []
    assert len(sess.resumed) == 1
    reason = sess.resumed[0][1]
    assert re.search(rf"\b0?{number}\b", reason) and word in reason
    t = saved(c)
    assert t.stage is Stage.PLAN and t.plan_retries == 1
    assert t.ticket_cursor == 0


# --- one pick per ticket ----------------------------------------------------

def test_a_ticket_keeps_its_pick_across_its_own_sessions_and_the_next_chooses(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture",
                  [(), ("frontend",), ()])
    out = launched(step(c, wt, PLAN_DONE))
    usage_now(monkeypatch, deny(ahead(), FABLE))
    out += launched(step(c, wt, IMPL_DONE))
    assert out == [(ASTRA, "medium"), (f"anthropic/{OPUS}", "medium")]
    assert saved(c).picks["implement"] == f"anthropic/{OPUS}@medium"
    # ticket 2 parks in its gate loop; fable is admitted again
    state.save(c.state_dir, dc_replace(saved(c), park=PARK_WAKE))
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    usage_now(monkeypatch, ahead())
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.spawned == []
    assert [(r[2], r[3]) for r in sess.resumed] == [
        (f"anthropic/{OPUS}", "medium")]
    assert saved(c).picks["implement"] == f"anthropic/{OPUS}@medium"
    # ticket 2 done: ticket 3 chooses again from the architecture list
    sess = step(c, wt, IMPL_DONE)
    assert launched(sess) == [(ASTRA, "medium")]
    assert saved(c).picks["implement"] == f"{ASTRA}@medium"


def test_a_ticket_that_cannot_run_holds_the_tickets_after_it(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture",
                  [(), ("frontend",), ()])
    assert launched(step(c, wt, PLAN_DONE)) == [(ASTRA, "medium")]
    usage_now(monkeypatch, deny(ahead(), FABLE, OPUS))
    sess = step(c, wt, IMPL_DONE)
    assert sess.spawned == [] and sess.resumed == []
    t = saved(c)
    assert t.stage is Stage.IMPLEMENT and t.ticket_cursor == 1
    assert "implement" not in t.picks


def test_an_override_of_a_pin_covers_one_ticket(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture",
                  [(), ("frontend",), (), ("frontend",)])
    out = launched(step(c, wt, PLAN_DONE))
    usage_now(monkeypatch, deny(ahead(), FABLE, OPUS))
    assert step(c, wt, IMPL_DONE).spawned == []
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(model=ASTRA, bypass_usage=False))
    out += launched(step(c, wt, IMPL_DONE))
    assert saved(c).ticket_cursor == 2
    out += launched(step(c, wt, IMPL_DONE))
    assert saved(c).ticket_cursor == 3
    assert [m for m, _ in out] == [ASTRA] * 3 and out[2][1] == "medium"
    assert step(c, wt, IMPL_DONE).spawned == []
    t = saved(c)
    assert t.stage is Stage.IMPLEMENT and t.ticket_cursor == 3


def test_a_track_no_longer_pinned_parks_the_task_for_the_operator(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture",
                  [(), ("frontend",), ()])
    assert launched(step(c, wt, PLAN_DONE)) == [(ASTRA, "medium")]
    c2 = dc_replace(c, models=policy(pinned=["security", "architecture"]))
    sess = step(c2, wt, IMPL_DONE)
    assert sess.spawned == []
    t = saved(c)
    assert t.park == PARK_HUMAN and t.ticket_cursor == 1
    assert re.search(r"\b0?2\b", t.park_note) and "frontend" in t.park_note


def test_a_later_edit_of_a_ticket_file_does_not_move_the_ticket(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ()],
                  mode="openai")
    assert launched(step(c, wt, PLAN_DONE)) == [(ASTRA, "medium")]
    assert tracks_of(c) == {}
    write_ticket(wt, 2, "frontend")
    assert launched(step(c, wt, IMPL_DONE)) == [(ASTRA, "medium")]


def test_removing_the_track_line_later_does_not_move_the_ticket(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)],
                  mode="openai")
    step(c, wt, PLAN_DONE)
    write_ticket(wt, 2)
    assert launched(step(c, wt, IMPL_DONE)) == [
        (f"anthropic/{FABLE}", "medium")]


# --- status line --------------------------------------------------------------

def test_status_line_between_two_tickets_names_the_next_tickets_entry(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    make_task(c, issue=42, stage=Stage.IMPLEMENT, track="architecture",
              ticket_cursor=1, ticket_count=3, ticket_tracks={2: "frontend"})
    assert f"implement [anthropic/{FABLE}@medium]" in main._status_lines(c)[0]
