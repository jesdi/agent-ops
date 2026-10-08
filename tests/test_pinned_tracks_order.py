"""Acceptance tests for ticket 01 of pinned-tracks: the priority mode does not
reorder a pinned track. Black-box through run_pass (launches read from the
sessions fake and the task state), load_config, triage.run_sweep and the
dispatcher's status lines. Policy and "anthropic is ahead" are the ones that
specs/pinned-tracks/spec.md defines at its top.

One frozen NOW per test (also frozen inside run_pass and the sweep), so reset
distances are exact. Pace margin 1.0 admits every weekly window with quota
left; a model is denied with a scoped weekly window that is 100% used."""
import json
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import dispatcher.main as main
from dispatcher import execution_overrides, priority, triage
from dispatcher.config import load_config
from dispatcher.models import parse_policy
from dispatcher.state import PARK_WAKE, Stage, load
from dispatcher.usage import PaceConfig
from tests.test_config import SAMPLE
from tests.test_main import (FakeSessions, cfg, deps, make_task, valid_spec,
                             write_tickets)
from tests.test_priority_auto import both, usage, wk
from tests.test_triage import BLOB, OLD, RESULT, FakeDeps, _sweep_cfg

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
PACE = PaceConfig(weekend_weight=1.0, pace_margin=1.0)
FABLE, OPUS, SONNET = "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"
ASTRA, SOL, LUNA = "openai/gpt-astra", "openai/gpt-sol", "openai/gpt-luna"
PINNED = ["security", "architecture", "frontend"]


def _track(spec, plan, implement, review, when="x"):
    return {"when": when, "spec": spec, "plan": plan, "implement": implement,
            "review": review}


def policy_raw(pinned=PINNED, triage_list=None):
    raw = {
        "triage": triage_list or [f"{SONNET}@medium"],
        "untracked": "standard",
        "tracks": {
            "standard": _track(
                [f"{FABLE}@medium", f"{ASTRA}@medium"],
                [f"{FABLE}@medium", f"{ASTRA}@medium"],
                [f"{SONNET}@medium", f"{SOL}@medium"],
                [f"{ASTRA}@medium", f"{OPUS}@medium"]),
            "frontend": _track(
                [f"{FABLE}@medium", f"{OPUS}@medium"],
                [f"{FABLE}@medium", f"{OPUS}@medium"],
                [f"{FABLE}@medium", f"{OPUS}@medium"],
                [f"{ASTRA}@medium", f"{OPUS}@medium"]),
            "architecture": _track(
                [f"{ASTRA}@high", f"{FABLE}@high"],
                [f"{ASTRA}@high", f"{FABLE}@high"],
                [f"{ASTRA}@medium", f"{FABLE}@medium"],
                [f"{FABLE}@high", f"{ASTRA}@high"]),
            "security": _track(
                [f"{ASTRA}@high"], [f"{ASTRA}@high"], [f"{ASTRA}@high"],
                [f"{FABLE}@high", f"{ASTRA}@high"]),
        },
    }
    if pinned is not None:
        raw["pinned"] = pinned
    return raw


def policy(**kw):
    return parse_policy(policy_raw(**kw))


def ahead():
    """Anthropic 20% used, resets in 1 day (5.6x); openai 10% used, 4 days (1.6x)."""
    return both([wk(0.20, 24, now=NOW)], [wk(0.10, 96, now=NOW)])


def deny(usages, *scopes):
    for scope in scopes:
        prov = "openai" if scope.startswith("gpt-") else "anthropic"
        u = usages[prov]
        usages[prov] = usage(prov, *u.windows, wk(1.0, 84, scope, now=NOW))
    return usages


def save_mode(c, mode):
    d = Path(c.state_dir)
    d.mkdir(parents=True, exist_ok=True)
    priority.save(str(d), mode, actor="op", now=NOW)


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def make_cfg(tmp_path, monkeypatch, usages, pinned=PINNED, mode=None, pace=PACE,
             models=None):
    c = dc_replace(cfg(tmp_path), pace=pace,
                   models=models or policy(pinned=pinned))
    monkeypatch.setattr(main, "fetch_all", lambda cfg, **k: usages)
    monkeypatch.setattr(main, "datetime", Frozen)
    if mode:
        save_mode(c, mode)
    return c


def enter(c, issue, track, stage, **kw):
    """A task whose previous stage just signalled done: PLAN done enters
    implement; IMPLEMENT done (one ticket) enters review."""
    wt = make_task(c, issue=issue, stage=stage, track=track, **kw)
    write_tickets(wt, 1)
    if stage is Stage.PLAN:
        sig = {"stage": "plan", "status": "done", "note": "1 ticket",
               "artifact": ".agent/tickets"}
    else:
        sig = {"stage": "implement", "status": "done"}
    (wt / ".agent" / "stage.json").write_text(json.dumps(sig))


def run_pass(c, *issues):
    sess = FakeSessions(alive=set(issues))
    main.run_pass(c, deps(sess=sess))
    return sess


def launched(sess, issue=None):
    return [(s[2], s[4]) for s in sess.spawned if issue in (None, s[0])]


def implement_on(tmp_path, monkeypatch, track, usages, **kw):
    c = make_cfg(tmp_path, monkeypatch, usages, **kw)
    enter(c, 42, track, Stage.PLAN)
    return c, run_pass(c, 42)


def review_on(tmp_path, monkeypatch, track, usages, implement_pick, **kw):
    c = make_cfg(tmp_path, monkeypatch, usages, **kw)
    enter(c, 42, track, Stage.IMPLEMENT, ticket_cursor=1, ticket_count=1,
          picks={"implement": implement_pick})
    return c, run_pass(c, 42)


# --- config ----------------------------------------------------------------

def _load(tmp_path, pinned_yaml):
    import yaml
    raw = policy_raw(pinned=None)
    text = SAMPLE + "models:\n" + "\n".join(
        "  " + ln for ln in yaml.safe_dump(raw).splitlines()) + "\n"
    if pinned_yaml is not None:
        text += f"  pinned: {pinned_yaml}\n"
    p = tmp_path / "targets.yaml"
    p.write_text(text)
    return load_config(p)


def test_a_valid_pinned_list_loads(tmp_path):
    assert _load(tmp_path, "[security, architecture]").models.pinned == (
        "security", "architecture")


def test_pinned_naming_an_unknown_track_fails_the_load(tmp_path):
    with pytest.raises(ValueError, match=r"(?s)(?=.*pinned)(?=.*backend)"):
        _load(tmp_path, "[security, backend]")


def test_pinned_naming_a_track_twice_fails_the_load(tmp_path):
    with pytest.raises(ValueError, match=r"(?s)(?=.*pinned)(?=.*frontend)"):
        _load(tmp_path, "[frontend, security, frontend]")


def test_pinned_that_is_not_a_list_fails_the_load_naming_pinned(tmp_path):
    _load(tmp_path, "[frontend]")  # control: the key itself is accepted
    with pytest.raises(ValueError, match="pinned"):
        _load(tmp_path, "frontend")


# --- unpinned behavior holds -------------------------------------------------

@pytest.mark.parametrize("pinned", [None, []], ids=["no-key", "empty-list"])
def test_with_no_pinned_track_the_mode_orders_architecture(
        tmp_path, monkeypatch, pinned):
    _, sess = implement_on(tmp_path, monkeypatch, "architecture", ahead(),
                           pinned=pinned)
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]


def test_a_track_the_pinned_list_does_not_name_is_ordered_by_the_mode(
        tmp_path, monkeypatch):
    _, sess = review_on(tmp_path, monkeypatch, "standard", ahead(), "")
    assert launched(sess) == [(f"anthropic/{OPUS}", "medium")]


def test_triage_is_still_ordered_by_the_mode(tmp_path):
    c = dc_replace(_sweep_cfg(tmp_path), pace=PACE, models=policy(
        triage_list=[f"{SONNET}@medium", f"{LUNA}@medium"]))
    save_mode(c, "openai")
    triage.save_cursors(tmp_path, {"o/a": OLD})
    seen = []

    def fake_session(cfg, repo, blob, started_date, entry, run=None):
        seen.append(str(entry))
        return {"issues": []}

    with patch.object(triage, "fetch_all", return_value=ahead()), \
         patch.object(triage, "datetime", Frozen), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", side_effect=fake_session), \
         patch.object(triage.triage_apply, "apply", return_value=RESULT):
        triage.run_sweep(c, FakeDeps())
    assert seen == [LUNA + "@medium"]


# --- the mode does not reorder a pinned track ---------------------------------

def test_auto_does_not_reorder_a_pinned_track(tmp_path, monkeypatch):
    _, sess = implement_on(tmp_path, monkeypatch, "architecture", ahead())
    assert launched(sess) == [(ASTRA, "medium")]


def test_a_provider_first_mode_does_not_reorder_a_pinned_track(
        tmp_path, monkeypatch):
    _, sess = implement_on(tmp_path, monkeypatch, "architecture", ahead(),
                           mode="anthropic")
    # PLAN done enters implement; the plan stage itself is covered below
    assert launched(sess) == [(ASTRA, "medium")]


def test_mode_anthropic_does_not_reorder_the_plan_stage_of_a_pinned_track(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="anthropic")
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW,
                   track="architecture")
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": "spec.md",
         "track": "architecture"}))
    sess = run_pass(c, 42)
    assert launched(sess) == [(ASTRA, "high")]


def test_the_session_bound_rule_does_not_reorder_a_pinned_track(
        tmp_path, monkeypatch):
    pace = PaceConfig(weekend_weight=1.0, pace_margin=1.0,
                      session_week_share={"anthropic": 0.05})
    u = both([wk(0.20, 10, now=NOW)], [wk(0.10, 96, now=NOW)])
    _, sess = implement_on(tmp_path, monkeypatch, "architecture", u, pace=pace)
    assert launched(sess) == [(ASTRA, "medium")]


def test_an_unpinned_track_is_ordered_by_the_mode_in_the_same_pass(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    for issue, track in ((42, "architecture"), (43, "standard")):
        enter(c, issue, track, Stage.IMPLEMENT, ticket_cursor=1, ticket_count=1)
    sess = run_pass(c, 42, 43)
    assert launched(sess, 42) == [(f"anthropic/{FABLE}", "high")]
    assert launched(sess, 43) == [(f"anthropic/{OPUS}", "medium")]


# --- the gate, picks and overrides --------------------------------------------

def test_a_pinned_track_falls_back_in_written_order(tmp_path, monkeypatch):
    _, sess = implement_on(tmp_path, monkeypatch, "architecture",
                           deny(ahead(), "gpt-astra"), mode="anthropic")
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]


def test_a_pinned_track_waits_when_none_of_its_entries_is_admitted(
        tmp_path, monkeypatch):
    c, sess = implement_on(tmp_path, monkeypatch, "frontend",
                           deny(ahead(), "claude-fable-5-1", "claude-opus-5"),
                           mode="openai")
    assert sess.spawned == [] and sess.resumed == []


def test_a_single_entry_pinned_list_waits(tmp_path, monkeypatch):
    c, sess = implement_on(tmp_path, monkeypatch, "security",
                           deny(ahead(), "gpt-astra"))
    assert sess.spawned == [] and sess.resumed == []


def test_a_pick_on_a_pinned_track_is_kept(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    make_task(c, issue=42, stage=Stage.PLAN, track="architecture",
              park=PARK_WAKE, picks={"plan": f"{FABLE}@high"})
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [(r[2], r[3]) for r in sess.resumed] == [
        (f"anthropic/{FABLE}", "high")]


def test_a_one_shot_override_wins_on_a_pinned_track(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    enter(c, 42, "frontend", Stage.PLAN)
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(model=ASTRA, bypass_usage=False))
    sess = run_pass(c, 42)
    assert [m for m, _ in launched(sess)] == [ASTRA]


# --- review independence on the written order ----------------------------------

def test_review_of_a_pinned_track_avoids_the_implement_provider(
        tmp_path, monkeypatch):
    _, sess = review_on(tmp_path, monkeypatch, "architecture", ahead(),
                        f"{FABLE}@medium")
    assert launched(sess) == [(ASTRA, "high")]


def test_review_of_a_pinned_track_keeps_the_written_order_when_first_is_independent(
        tmp_path, monkeypatch):
    _, sess = review_on(tmp_path, monkeypatch, "architecture", ahead(),
                        f"{ASTRA}@medium", mode="openai")
    assert launched(sess) == [(f"anthropic/{FABLE}", "high")]


# --- a target's own policy -------------------------------------------------------

def test_a_targets_own_pinned_list_applies_and_the_global_one_does_not(
        tmp_path, monkeypatch):
    own = policy_raw(pinned=["standard"])
    own["tracks"]["standard"]["implement"] = [f"{SOL}@medium", f"{SONNET}@medium"]
    base = cfg(tmp_path)
    c = make_cfg(tmp_path, monkeypatch, ahead(), models=policy())
    c = dc_replace(c, targets=[dc_replace(
        base.targets[0], models=parse_policy(own))])
    enter(c, 42, "standard", Stage.PLAN)
    enter(c, 43, "architecture", Stage.PLAN)
    sess = run_pass(c, 42, 43)
    assert launched(sess, 42) == [(SOL, "medium")]
    assert launched(sess, 43) == [(f"anthropic/{FABLE}", "medium")]


# --- status line ------------------------------------------------------------------

def test_status_line_of_a_pinned_task_with_no_pick_names_the_written_first_entry(
        tmp_path):
    c = dc_replace(cfg(tmp_path), pace=PACE, models=policy())
    make_task(c, issue=42, stage=Stage.IMPLEMENT, track="architecture")
    save_mode(c, "anthropic")
    assert f"implement [{ASTRA}@medium]" in main._status_lines(c)[0]
