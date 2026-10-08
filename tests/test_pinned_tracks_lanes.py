"""Acceptance tests for ticket 02 of pinned-tracks: the example config carries
the operator's lanes, and triage and the spec session are told how to choose.
Black-box: load_config on targets.example.yaml, the prompt a triage sweep
receives (run_sweep) and the prompt a spec launch receives (run_pass)."""
import subprocess
from dataclasses import replace as dc_replace
from pathlib import Path
from unittest.mock import patch

import dispatcher.main as main
from dispatcher import triage
from dispatcher.config import load_config
from dispatcher.github import Candidate
from dispatcher.models import parse_policy, tracks_text
from tests.test_main import (FakeGitHub, FakeSessions, cfg, deps,
                             patch_usage, patch_workspace)
from tests.test_triage import BLOB, OK_USAGE, OLD, RESULT, FakeDeps, _sweep_cfg

EXAMPLE = Path(__file__).resolve().parent.parent / "targets.example.yaml"
FABLE, OPUS = "anthropic/claude-fable-5-1", "anthropic/claude-opus-5"
ASTRA = "openai/gpt-astra"


def example_policy():
    return load_config(EXAMPLE).models


def lists(policy, track, *stages):
    return {s: [str(e) for e in policy.tracks[track].stages[s]] for s in stages}


def _track(when="w"):
    e = ["claude-sonnet-5@medium"]
    return {"when": when, "spec": e, "plan": e, "implement": e, "review": e}


def lanes(pinned):
    """Tracks written in the order standard, frontend, architecture, security."""
    raw = {"triage": ["claude-sonnet-5@medium"], "untracked": "standard",
           "tracks": {n: _track(f"when-{n}") for n in
                      ("standard", "frontend", "architecture", "security")}}
    if pinned:
        raw["pinned"] = pinned
    return parse_policy(raw)


PINNED = ["security", "architecture", "frontend"]


def line_of(text, track):
    (line,) = [ln for ln in text.splitlines() if f"`{track}`" in ln]
    return line


# --- the example config ------------------------------------------------------

def test_example_config_loads_with_the_operator_pinned_list():
    policy = example_policy()
    assert list(policy.pinned) == PINNED
    assert "trivial" not in policy.pinned and "standard" not in policy.pinned


def test_example_architecture_track_has_the_spec_lists():
    policy = example_policy()
    assert lists(policy, "architecture", "spec", "plan", "implement", "review") == {
        "spec": [f"{ASTRA}@high", f"{FABLE}@high"],
        "plan": [f"{ASTRA}@high", f"{FABLE}@high"],
        "implement": [f"{ASTRA}@medium", f"{FABLE}@medium"],
        "review": [f"{FABLE}@high", f"{ASTRA}@high"],
    }
    when = policy.tracks["architecture"].when.lower()
    assert "backend" in when and "architecture" in when


def test_example_security_track_is_openai_only_until_review():
    got = lists(example_policy(), "security", "spec", "plan", "implement", "review")
    for stage in ("spec", "plan", "implement"):
        assert got[stage] == [f"{ASTRA}@high"]
    assert got["review"] == [f"{FABLE}@high", f"{ASTRA}@high"]


def test_example_frontend_track_is_anthropic_only_until_review():
    got = lists(example_policy(), "frontend", "spec", "plan", "implement")
    for stage in ("spec", "plan", "implement"):
        assert got[stage] == [f"{FABLE}@medium", f"{OPUS}@medium"]


# --- the track list ----------------------------------------------------------

def test_track_list_shows_pinned_first_in_pinned_order_and_marks_them():
    text = tracks_text(lanes(PINNED))
    lines = [ln for ln in text.splitlines()
             if any(f"`{n}`" in ln for n in ("security", "architecture",
                                             "frontend", "standard"))]
    names = [n for ln in lines for n in ("security", "architecture", "frontend",
                                         "standard") if f"`{n}`" in ln]
    assert names == ["security", "architecture", "frontend", "standard"]
    for n in PINNED:
        assert "pinned" in line_of(text, n).lower()
    assert "pinned" not in line_of(text, "standard").lower()


def test_track_list_gives_the_choosing_rules():
    text = tracks_text(lanes(PINNED)).lower()
    assert "clearly" in text
    assert "`standard`" in text.split("clearly", 1)[1]  # the untracked fallback
    assert "first" in text


def test_track_list_with_no_pinned_track_is_as_today():
    assert tracks_text(lanes(None)) == (
        "- `standard`: when-standard\n- `frontend`: when-frontend\n"
        "- `architecture`: when-architecture\n- `security`: when-security")


# --- the prompts -------------------------------------------------------------

def test_triage_prompt_contains_the_track_list(tmp_path):
    policy = lanes(PINNED)
    c = dc_replace(_sweep_cfg(tmp_path), models=policy)
    triage.save_cursors(tmp_path, {"o/a": OLD})
    real = triage._run_session
    prompt = []

    def session(cfg, repo, blob, started_date, entry, run=None):
        def fake_run(cmd, capture_output, text, timeout):
            d = Path(cfg.state_dir) / triage.TRIAGE_DIR
            (d / "o-a-2026-07-30.json").write_text('{"issues": []}')
            prompt.append((d / "o-a-2026-07-30-prompt.md").read_text())
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cfg, repo, blob, "2026-07-30", entry, run=fake_run)

    with patch.object(triage, "fetch_all", return_value=OK_USAGE), \
         patch.object(triage, "_run_session", side_effect=session), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage.triage_apply, "apply", return_value=RESULT):
        triage.run_sweep(c, FakeDeps())
    assert prompt and tracks_text(policy) in prompt[0]
    assert "pinned" in line_of(prompt[0], "security").lower()


def test_spec_prompt_contains_the_track_list(tmp_path, monkeypatch):
    policy = lanes(PINNED)
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), models=policy)
    gh = FakeGitHub([Candidate(42, "T", "u42")])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert sess.spawned and tracks_text(policy) in sess.spawned[0][3]
    assert "pinned" in line_of(sess.spawned[0][3], "security").lower()
