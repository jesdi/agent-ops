"""Acceptance tests for ticket 03 (provider priority): a stored box-wide
priority mode (`<state_dir>/provider-priority.json`) puts one provider's
entries first on every list the box routes; the usage gate still decides what
may run. Black-box: run_pass launches, triage.run_sweep (model it ran on and
its skip note), main._status_lines, dispatcher.priority.load/save.

One frozen NOW per test (also frozen inside run_pass / the sweep), so reset
distances are exact. Admission is not under test unless a case says so: pace
margin 1.0 admits every weekly window with quota left. Individual entries are
denied with a scoped weekly window that is 100% used and names only that
model (a scope applies to models whose id contains it)."""
import json
import os
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import dispatcher.main as main
from dispatcher import execution_overrides, triage
from dispatcher.models import parse_policy
from dispatcher.state import Stage
from dispatcher.usage import PaceConfig, admits, unavailable
from tests.test_main import cfg, make_task
from tests.test_priority_auto import (FLIPPED, LUNA, OPUS, SOL, SONNET, WRITTEN,
                                      PACE, both, launched, run, ses, usage, wk)
from tests.test_priority_session_bound import OPENAI_5_6, SHARE, go, pace
from tests.test_triage import (BLOB, OLD, RESULT, FakeDeps, _sweep_cfg)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)  # a Thursday
ROUTED = {"anthropic", "openai"}
SONNET_ID, OPUS_ID = "anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5"
LUNA_ID, SOL_ID = "openai/gpt-6-luna", "openai/gpt-6-sol"
FILE = "provider-priority.json"


def state_dir(tmp_path):
    return Path(cfg(tmp_path).state_dir)  # the dir run_pass will use


def save_mode(tmp_path, mode="openai"):
    from dispatcher import priority
    d = state_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    priority.save(str(d), mode, actor="op", now=NOW)


def write_raw(tmp_path, text):
    d = state_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / FILE).write_text(text)
    return d / FILE


# anthropic 6.3x (10% used, resets in 1 day) vs openai 1.0x (50%, 84 h)
def anthropic_hungry():
    return both([wk(0.10, 24, now=NOW)], [wk(0.50, 84, now=NOW)])


def go_hungry(tmp_path, monkeypatch, **kw):
    return run(tmp_path, monkeypatch, anthropic_hungry(), frozen=NOW, **kw)


# --- launch criteria -------------------------------------------------------

def test_mode_openai_beats_anthropic_6_3x_vs_openai_1_0x(tmp_path, monkeypatch):
    save_mode(tmp_path)
    sess, t = go_hungry(tmp_path, monkeypatch)
    assert launched(sess) == [LUNA_ID]
    assert sess.spawned[0][4] == "high"
    assert t.picks["implement"] == LUNA


FOUR = [OPUS, SOL, SONNET, LUNA]


@pytest.mark.parametrize("denied,expected", [
    ([], SOL_ID),
    (["gpt-6-sol"], LUNA_ID),
    (["gpt-6-sol", "gpt-6-luna"], OPUS_ID),
    (["gpt-6-sol", "gpt-6-luna", "opus"], SONNET_ID),
], ids=["none", "sol", "sol+luna", "sol+luna+opus"])
def test_openai_entries_first_then_the_rest_each_group_in_written_order(
        tmp_path, monkeypatch, denied, expected):
    save_mode(tmp_path)
    scoped = {"gpt-6-sol": "openai", "gpt-6-luna": "openai",
              "opus": "anthropic", "sonnet": "anthropic"}
    u = both([wk(0.10, 24, now=NOW)], [wk(0.50, 84, now=NOW)])
    for scope in denied:
        p = usage(scoped[scope], *u[scoped[scope]].windows,
                  wk(1.0, 84, scope, now=NOW))
        u[scoped[scope]] = p
    sess, _ = run(tmp_path, monkeypatch, u, implement=FOUR, frozen=NOW)
    assert launched(sess) == [expected]


FABLE = "claude-fable-5-1"
FABLE_ID = "anthropic/claude-fable-5-1"


def routed_openai_policy(monkeypatch, implement):
    """Openai stays routed (via the triage list) while the stage list under
    test may hold no openai entry, so the stored mode really reads `openai`."""
    import tests.test_priority_auto as auto
    track = {"when": "x", "spec": implement, "plan": implement,
             "implement": implement, "review": implement}
    monkeypatch.setattr(auto, "policy", lambda impl, review=None: parse_policy({
        "triage": [SONNET, "openai/gpt-6-luna"], "untracked": "standard",
        "tracks": {"standard": track}}))


def fable_lower_pace(openai_windows):
    # unscoped anthropic 6.3x, Fable-scoped 1.0x: fable's pace is the lower
    return both([wk(0.10, 24, now=NOW), wk(0.50, 84, "Fable", now=NOW)],
                openai_windows)


def test_mode_openai_leaves_a_list_with_no_openai_entry_unchanged(
        tmp_path, monkeypatch):
    from dispatcher import priority
    routed_openai_policy(monkeypatch, [FABLE, OPUS])
    save_mode(tmp_path)
    u = fable_lower_pace([wk(0.50, 84, now=NOW)])
    sess, _ = run(tmp_path, monkeypatch, u, frozen=NOW)
    assert priority.load(str(state_dir(tmp_path)), ROUTED) == "openai"
    assert launched(sess) == [FABLE_ID]           # auto would rank opus first


def test_mode_openai_keeps_written_order_inside_a_group_despite_required_pace(
        tmp_path, monkeypatch):
    routed_openai_policy(monkeypatch, [FABLE, OPUS, SOL])
    save_mode(tmp_path)
    u = fable_lower_pace([wk(0.50, 84, now=NOW), ses(0.95, 2, NOW)])
    sess, _ = run(tmp_path, monkeypatch, u, frozen=NOW)
    assert launched(sess) == [FABLE_ID]           # openai denied; not opus


def test_mode_openai_gate_denies_luna_falls_to_sonnet_verdict_as_under_auto(
        tmp_path, monkeypatch):
    save_mode(tmp_path)
    u = both([wk(0.10, 24, now=NOW)], [wk(0.90, 84, now=NOW)])
    tight = PaceConfig(weekend_weight=1.0, pace_margin=0.0)
    sess, _ = run(tmp_path, monkeypatch, u, frozen=NOW, pace=tight)
    assert launched(sess) == [SONNET_ID]
    # the verdict is a function of usage alone: pinned, identical in every mode
    v = admits(u, "openai/gpt-6-luna", NOW, tight)
    assert (v.admitted, v.reason, v.binding.window.kind.value) == (
        False, "over-pace", "weekly")
    assert v.binding.allowance == pytest.approx(0.5)
    assert v.binding.headroom == pytest.approx(-0.4)
    from dispatcher import priority
    assert priority.load(str(state_dir(tmp_path)), ROUTED) == "openai"


def test_mode_openai_with_openai_usage_unavailable_launches_sonnet(
        tmp_path, monkeypatch):
    save_mode(tmp_path)
    u = {"anthropic": usage("anthropic", wk(0.40, 84, now=NOW)),
         "openai": unavailable("openai", 0.0)}
    sess, _ = run(tmp_path, monkeypatch, u, frozen=NOW)
    assert launched(sess) == [SONNET_ID]
    from dispatcher import priority
    assert priority.load(str(state_dir(tmp_path)), ROUTED) == "openai"


def test_mode_openai_gate_denying_both_entries_launches_nothing_no_pick(
        tmp_path, monkeypatch):
    save_mode(tmp_path)
    u = both([wk(0.10, 24, now=NOW), ses(0.95, 2, NOW)],
             [wk(0.50, 84, now=NOW), ses(0.95, 2, NOW)])
    sess, t = run(tmp_path, monkeypatch, u, frozen=NOW)
    assert sess.spawned == [] and "implement" not in t.picks
    from dispatcher import priority
    assert priority.load(str(state_dir(tmp_path)), ROUTED) == "openai"


def test_mode_openai_review_avoids_an_openai_implement_pick(
        tmp_path, monkeypatch):
    save_mode(tmp_path)
    sess, _ = go_hungry(tmp_path, monkeypatch, review=[OPUS, SOL],
                        stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=1,
                        picks={"implement": LUNA})
    assert launched(sess) == [OPUS_ID]


def test_mode_openai_review_after_a_sonnet_implement_pick_goes_to_openai(
        tmp_path, monkeypatch):
    save_mode(tmp_path)
    sess, _ = go_hungry(tmp_path, monkeypatch, review=[OPUS, SOL],
                        stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=1,
                        picks={"implement": SONNET})
    assert launched(sess) == [SOL_ID]


def test_mode_openai_orders_a_targets_own_policy(tmp_path, monkeypatch):
    save_mode(tmp_path)
    own = parse_policy({"triage": [SONNET], "untracked": "standard", "tracks": {
        "standard": {"when": "x", "spec": [OPUS, SOL], "plan": [OPUS, SOL],
                     "implement": [OPUS, SOL], "review": [OPUS, SOL]}}})
    c = cfg(tmp_path)
    c = dc_replace(c, pace=PACE,
                   targets=[dc_replace(c.targets[0], models=own)])
    monkeypatch.setattr(main, "fetch_all",
                        lambda cfg, **k: anthropic_hungry())

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(main, "datetime", Frozen)
    from tests.test_main import FakeSessions, deps, write_tickets
    wt = make_task(c, issue=42, stage=Stage.PLAN, picks={})
    write_tickets(wt, 1)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "plan", "status": "done", "note": "1 ticket",
        "artifact": ".agent/tickets"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert launched(sess) == [SOL_ID]


def test_mode_openai_beats_a_session_bound_anthropic(tmp_path, monkeypatch):
    save_mode(tmp_path)
    sess, _ = go(tmp_path, monkeypatch,
                 [ses(0.0, 5, NOW), wk(0.70, 30, now=NOW)])
    assert launched(sess) == [LUNA_ID]


def test_after_the_mode_changed_the_next_ticket_and_review_follow_the_mode(
        tmp_path, monkeypatch):
    # auto: anthropic 6.3x wins implement; then the operator switches to openai
    sess, t = go_hungry(tmp_path, monkeypatch)
    assert launched(sess) == [SONNET_ID]
    assert t.picks["implement"] == SONNET_ID
    save_mode(tmp_path)
    sess, _ = go_hungry(tmp_path, monkeypatch, stage=Stage.IMPLEMENT,
                        ticket_cursor=1, ticket_count=2, picks=t.picks)
    assert launched(sess) == [LUNA_ID]            # the next ticket chooses again
    sess, _ = go_hungry(tmp_path, monkeypatch, review=[OPUS, SOL],
                        stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=1,
                        picks=t.picks)
    assert launched(sess) == [SOL_ID]             # fresh stage follows the mode


def test_mode_openai_one_shot_override_wins(tmp_path, monkeypatch):
    save_mode(tmp_path)
    ov = execution_overrides.ExecutionOverride(model=OPUS, bypass_usage=False)
    sess, _ = go_hungry(tmp_path, monkeypatch, override=ov)
    assert launched(sess) == [OPUS_ID]


# --- fallbacks: every one behaves as auto ----------------------------------

def auto_wins():  # anthropic 10% used / 4 d, openai 20% / 1 d: openai in auto
    return both([wk(0.10, 96, now=NOW)], [wk(0.20, 24, now=NOW)])


@pytest.mark.parametrize("text", [
    None,
    '{"mode": "open',
    json.dumps({"mode": "nvidia", "set_by": "op", "set_at": "x"}),
], ids=["no-file", "truncated", "unrouted-nvidia"])
def test_a_missing_or_unreadable_or_unrouted_mode_behaves_as_auto(
        tmp_path, monkeypatch, text):
    from dispatcher import priority
    path = state_dir(tmp_path) / FILE
    if text is not None:
        write_raw(tmp_path, text)
    sess, _ = run(tmp_path, monkeypatch, auto_wins(), frozen=NOW)
    assert launched(sess) == [LUNA_ID]
    assert priority.load(str(state_dir(tmp_path)), ROUTED) == "auto"
    if text is None:
        assert not path.exists()
    else:
        assert path.read_text() == text            # a fallback never rewrites


# --- load / save -----------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"mode": "open', '{"mode": 3}', '{"mode": null}', '{"mode": ["openai"]}',
    '{"set_by": "op"}', '[]', '',
], ids=["truncated", "int", "null", "list", "no-mode", "not-object", "empty"])
def test_load_reads_auto_for_an_unreadable_file_and_leaves_it_alone(
        tmp_path, text):
    from dispatcher import priority
    p = write_raw(tmp_path, text)
    before = p.read_bytes()
    assert priority.load(str(state_dir(tmp_path)), ROUTED) == "auto"
    assert p.read_bytes() == before


def test_load_reads_auto_for_a_missing_file_and_creates_none(tmp_path):
    from dispatcher import priority
    d = state_dir(tmp_path)
    d.mkdir(parents=True)
    assert priority.load(str(d), ROUTED) == "auto"
    assert list(d.iterdir()) == []


def test_load_reads_auto_for_a_provider_not_routed_and_the_mode_returns_with_it(
        tmp_path):
    from dispatcher import priority
    save_mode(tmp_path)
    p = state_dir(tmp_path) / FILE
    before = p.read_bytes()
    assert priority.load(str(state_dir(tmp_path)), {"anthropic"}) == "auto"
    assert p.read_bytes() == before
    assert priority.load(str(state_dir(tmp_path)), ROUTED) == "openai"


def test_save_then_load_returns_the_mode_and_writes_the_documented_file(
        tmp_path):
    from dispatcher import priority
    d = state_dir(tmp_path)
    d.mkdir(parents=True)
    priority.save(str(d), "openai", actor="jesdi", now=NOW)
    assert priority.load(str(d), ROUTED) == "openai"
    body = json.loads((d / FILE).read_text())
    assert set(body) == {"mode", "set_by", "set_at"}
    assert (body["mode"], body["set_by"]) == ("openai", "jesdi")
    assert datetime.fromisoformat(body["set_at"]) == NOW
    assert [p.name for p in d.iterdir()] == [FILE]   # no temp file left
    priority.save(str(d), "auto", actor="jesdi", now=NOW)
    assert priority.load(str(d), ROUTED) == "auto"


# --- survives a restart ----------------------------------------------------

def test_a_saved_mode_is_used_by_every_later_pass_with_no_further_write(
        tmp_path, monkeypatch):
    save_mode(tmp_path)
    p = state_dir(tmp_path) / FILE
    before, mtime = p.read_bytes(), os.stat(p).st_mtime_ns
    for _ in range(2):  # each run builds fresh deps: a restarted dispatcher
        sess, _ = go_hungry(tmp_path, monkeypatch)
        assert launched(sess) == [LUNA_ID]
    assert (p.read_bytes(), os.stat(p).st_mtime_ns) == (before, mtime)


# --- triage ----------------------------------------------------------------

def triage_cfg(tmp_path, ents=(SONNET, "openai/gpt-6-luna")):
    pol = parse_policy({"triage": list(ents), "untracked": "t", "tracks": {
        "t": {"when": "w", "spec": [SONNET], "plan": [SONNET],
              "implement": [SONNET], "review": [SONNET]}}})
    return dc_replace(_sweep_cfg(tmp_path), models=pol, pace=PACE)


def sweep(tmp_path, usages):
    c = triage_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": OLD})
    seen = []

    def fake_session(cfg, repo, blob, started_date, entry, run=None):
        seen.append(str(entry))
        return {"issues": []}

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    with patch.object(triage, "fetch_all", return_value=usages), \
         patch.object(triage, "datetime", Frozen), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", side_effect=fake_session), \
         patch.object(triage.triage_apply, "apply", return_value=RESULT):
        triage.run_sweep(c, deps)
    return seen, deps


def test_triage_runs_on_the_openai_entry_in_mode_openai_though_anthropic_has_the_higher_pace(
        tmp_path):
    from dispatcher import priority
    priority.save(str(tmp_path), "openai", actor="op", now=NOW)
    seen, _ = sweep(tmp_path, anthropic_hungry())
    assert seen == ["openai/gpt-6-luna"]


def test_triage_in_auto_runs_on_the_entry_with_the_higher_required_pace(
        tmp_path):
    from dispatcher import priority
    seen, _ = sweep(tmp_path, both([wk(0.50, 84, now=NOW)],
                                   [wk(0.20, 24, now=NOW)]))
    assert seen == ["openai/gpt-6-luna"]            # second written entry
    assert priority.load(str(tmp_path), ROUTED) == "auto"


def test_triage_skip_note_names_openais_window_in_mode_openai(tmp_path):
    from dispatcher import priority
    priority.save(str(tmp_path), "openai", actor="op", now=NOW)
    u = both([wk(0.10, 24, now=NOW), ses(0.95, 2, NOW)],
             [wk(0.50, 84, now=NOW), ses(0.95, 2, NOW)])
    seen, deps = sweep(tmp_path, u)
    note = "\n".join(deps.notifier.sent[0][1]["lines"])
    assert seen == [] and "usage gate" in note
    assert "openai" in note and "anthropic" not in note


# --- status line -----------------------------------------------------------

def test_status_line_of_a_task_with_no_pick_names_the_openai_entry_in_mode_openai(
        tmp_path):
    from tests.test_priority_auto import policy
    c = dc_replace(cfg(tmp_path), models=policy(WRITTEN), pace=PACE)
    make_task(c, issue=42, stage=Stage.IMPLEMENT)
    save_mode(tmp_path)
    line = main._status_lines(c)[0]
    assert "implement [openai/gpt-6-luna@high]" in line


# --- console to dispatcher, end to end -------------------------------------

def test_a_mode_posted_through_the_console_is_the_one_run_pass_launches_on(
        tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.webfakes import FakeSources, make_config, tracks_policy
    from web.app import create_app
    stages = {s: WRITTEN for s in ("spec", "plan", "implement", "review")}
    console = dc_replace(make_config(state_dir(tmp_path)),
                         models=tracks_policy(**stages))
    r = TestClient(create_app(console, FakeSources())).post(
        "/api/priority", json={"mode": "openai"},
        headers={"Tailscale-User-Login": "jesdi"})
    assert r.status_code == 200
    sess, t = go_hungry(tmp_path, monkeypatch)   # auto would launch sonnet
    assert launched(sess) == [LUNA_ID]
    assert t.picks["implement"] == LUNA
