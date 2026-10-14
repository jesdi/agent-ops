"""The console's model settings: a `models:` block stored in the state dir
that replaces the targets.yaml one for the dispatcher and the console."""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from dispatcher import main, model_settings
from dispatcher.config import apply_stored_models, load_config
from dispatcher.convergence import pass_lock
from dispatcher.models import Entry
from dispatcher.state import PARK_HUMAN, PARK_WAKE, WHY_CONFIG, Stage, load
from tests.test_config import SAMPLE
from tests.test_main import (ADMIT_ALL, DENY_ALL, FakeSessions, cfg, deps, patch_usage,
                             patch_workspace)
from tests.test_main import make_task as make_saved_task
from tests.webfakes import HEADERS, FakeSources, make_task, tracks_policy
from web import app as web_app
from web.app import create_app

BLOCK = {"triage": ["claude-sonnet-5"], "untracked": "fast",
         "tracks": {"fast": {"when": "Small work.", "spec": ["claude-sonnet-5@low"],
                             "plan": ["claude-sonnet-5"], "implement": ["openai/gpt-luna@high"],
                             "review": ["claude-opus-5"]}}}


def entry(provider, model, effort=""):
    return {"provider": provider, "model": model, "effort": effort}


SONNET = entry("anthropic", "claude-sonnet-5")
# BLOCK as the console sends it: structured entries, every key present.
BODY = {"triage": [SONNET], "untracked": "fast", "pinned": [], "review_second": None,
        "tracks": {"fast": {"when": "Small work.", "plan_review": True,
                            "spec": [entry("anthropic", "claude-sonnet-5", "low")],
                            "plan": [SONNET],
                            "implement": [entry("openai", "gpt-luna", "high")],
                            "review": [entry("anthropic", "claude-opus-5")]}}}


def save(models, revision=""):
    return {"revision": revision, "models": models}


ANTHROPIC_ONLY = lambda m: ADMIT_ALL(m) if m.startswith("anthropic/") else DENY_ALL(m)


def write_config(tmp_path, extra=""):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE + extra)
    return p


def state_dir(tmp_path):
    return tmp_path / "state"   # conftest's AGENT_OPS_STATE_DIR


def store(tmp_path, models):
    d = state_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / model_settings.FILE).write_text(json.dumps({"models": models}))


def applied(tmp_path, extra=""):
    """The config in force: targets.yaml with the stored block laid over it."""
    return apply_stored_models(load_config(write_config(tmp_path, extra)))


def test_a_stored_block_replaces_the_targets_yaml_one(tmp_path):
    model_settings.save(state_dir(tmp_path), BLOCK, actor="jesdi",
                        now=datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))
    assert list(load_config(write_config(tmp_path)).models.tracks) == ["standard"]
    cfg = applied(tmp_path)
    assert list(cfg.models.tracks) == ["fast"]
    origin = cfg.models_origin
    assert origin.state == model_settings.Stored("jesdi", "2026-10-14T09:00:00+00:00")
    assert (origin.source, origin.error, len(origin.revision)) == ("console", "", 64)


@pytest.mark.parametrize("models, why", [
    ({**BLOCK, "untracked": "nope"}, "untracked"),
    ({}, "must not be empty"),
    (None, "must be a mapping"),   # a null block is checked like any other
])
def test_a_bad_stored_block_falls_back_to_targets_yaml_and_says_why(tmp_path, models, why):
    store(tmp_path, models)
    cfg = applied(tmp_path)
    assert list(cfg.models.tracks) == ["standard"]
    assert cfg.models_origin.source == "targets.yaml"
    assert why in cfg.models_origin.error


def test_a_stored_block_must_keep_every_session_week_share_provider(tmp_path):
    store(tmp_path, {**BLOCK, "tracks": {"fast": {**BLOCK["tracks"]["fast"],
                                                  "implement": ["claude-sonnet-5"]}}})
    extra = yaml.safe_dump({"models": BLOCK, "session_week_share": {"openai": 0.1}})
    assert "session_week_share" in applied(tmp_path, extra).models_origin.error


def test_apply_stored_models_follows_the_file_as_it_is_now(tmp_path):
    """The dispatcher applies it again inside the pass lock, and the console
    on every route: a block stored after load_config is the one in force."""
    boot = load_config(write_config(tmp_path))
    store(tmp_path, BLOCK)
    assert list(apply_stored_models(boot).models.tracks) == ["fast"]
    (state_dir(tmp_path) / model_settings.FILE).unlink()
    assert apply_stored_models(boot).models == boot.models


def test_read_says_why_a_stored_file_is_unusable(tmp_path):
    path = tmp_path / model_settings.FILE
    assert model_settings.read(tmp_path) == ("", None)
    for text, why in (("{", "not JSON"), ("[]", "no models"), ("{}", "no models")):
        path.write_text(text)
        revision, found = model_settings.read(tmp_path)
        assert isinstance(found, model_settings.Rejected) and why in found.reason
        assert len(revision) == 64
    path.unlink()
    path.mkdir()   # a directory: reading it fails with an OSError
    revision, found = model_settings.read(tmp_path)
    assert isinstance(found, model_settings.Rejected) and "unreadable" in found.reason
    assert revision == model_settings.UNREADABLE


def test_an_entry_refuses_a_separator_inside_a_part():
    assert str(Entry("openai", "gpt-luna", "high")) == "openai/gpt-luna@high"
    with pytest.raises(ValueError, match="must not contain"):
        Entry("anthropic", "claude@x")


def rig(tmp_path, tasks=()):
    cfg = load_config(write_config(tmp_path))
    fake = FakeSources()
    fake.tasks_list = list(tasks)
    return cfg, fake, TestClient(create_app(cfg, fake))


def test_the_console_stores_shows_and_resets_the_block(tmp_path):
    cfg, fake, client = rig(tmp_path)
    view = client.get("/api/settings/models", headers=HEADERS).json()
    assert view["source"] == "targets.yaml"
    assert view["revision"] == ""
    assert view["models"]["tracks"]["standard"]["spec"] == [entry("anthropic", "claude-opus-5")]
    assert view["efforts"]["openai"][0] == "minimal"

    r = client.put("/api/settings/models", headers=HEADERS, json=save(BODY))
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "console"
    assert r.json()["set_by"] == "jesdi@github"
    assert r.json()["models"] == BODY
    assert r.json()["revision"] == model_settings.read(state_dir(tmp_path))[0]
    assert r.json()["known_models"] == {
        "anthropic": ["claude-opus-5", "claude-sonnet-5"], "openai": ["gpt-luna"]}
    # The console's own views follow at once; the next pass reads the file.
    usage = client.get("/api/usage", headers=HEADERS).json()
    assert usage["gate"]["model"] == "anthropic/claude-sonnet-5"
    assert usage["priority"]["options"] == ["auto", "anthropic", "openai"]
    assert list(applied(tmp_path).models.tracks) == ["fast"]
    assert [e[0] for e in fake.appended] == ["model-settings-set"]

    r = client.delete("/api/settings/models", headers=HEADERS,
                      params={"revision": r.json()["revision"]})
    assert r.json()["source"] == "targets.yaml"
    assert not (state_dir(tmp_path) / model_settings.FILE).exists()
    assert list(r.json()["models"]["tracks"]) == ["standard"]
    assert client.get("/api/usage", headers=HEADERS).json()["priority"]["options"] == \
        ["auto", "anthropic"]


@pytest.mark.parametrize("change, why", [
    ({"plan": [entry("anthropic", "claude-x", "huge")]}, "effort"),
    ({"plan": [entry("anthropic", "claude@x")]}, "must not contain"),
    ({"plan": []}, "non-empty list"),
])
def test_an_invalid_block_is_refused_and_nothing_is_stored(tmp_path, change, why):
    _, fake, client = rig(tmp_path)
    bad = {**BODY, "tracks": {"fast": {**BODY["tracks"]["fast"], **change}}}
    r = client.put("/api/settings/models", headers=HEADERS, json=save(bad))
    assert r.status_code == 422
    assert why in r.json()["detail"]
    assert not (state_dir(tmp_path) / model_settings.FILE).exists()
    assert fake.appended == []


def test_a_block_that_drops_a_track_in_use_is_refused(tmp_path):
    busy = make_task(issue=9, target="portfolio_eval", track="standard")
    done = make_task(issue=10, target="portfolio_eval", track="standard", stage=Stage.DONE)
    _, _, client = rig(tmp_path, [busy, done])
    r = client.put("/api/settings/models", headers=HEADERS, json=save(BODY))
    assert r.status_code == 422
    assert r.json()["detail"].endswith(
        "portfolio_eval#9 (track 'standard' is no longer configured (have ['fast']))")
    assert not (state_dir(tmp_path) / model_settings.FILE).exists()


def test_a_block_that_drops_the_provider_of_a_picked_stage_is_refused(tmp_path):
    """The next stage already runs on openai: with no openai entry left, no
    usage is read for it and the gate never admits the stage again."""
    store(tmp_path, BLOCK)
    picked = make_task(issue=9, target="portfolio_eval", track="fast",
                       stage=Stage.IMPLEMENT, picks={"implement": "openai/gpt-luna@high"})
    _, _, client = rig(tmp_path, [picked])
    revision = client.get("/api/settings/models", headers=HEADERS).json()["revision"]
    no_openai = {**BODY, "tracks": {"fast": {**BODY["tracks"]["fast"], "implement": [SONNET]}}}
    r = client.put("/api/settings/models", headers=HEADERS, json=save(no_openai, revision))
    assert r.status_code == 422
    assert "portfolio_eval#9 (its implement stage runs on openai, which no model entry" \
        in r.json()["detail"]


def test_a_save_waits_for_a_running_pass_and_gives_up_with_409(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "PASS_LOCK_WAIT_SECONDS", 0.2)
    _, fake, client = rig(tmp_path)
    with pass_lock(str(state_dir(tmp_path))):
        r = client.put("/api/settings/models", headers=HEADERS, json=save(BODY))
    assert r.status_code == 409
    assert "dispatcher pass" in r.json()["detail"]
    assert not (state_dir(tmp_path) / model_settings.FILE).exists()
    assert fake.appended == []


def test_a_save_or_reset_from_a_stale_page_is_refused(tmp_path):
    """Two tabs read the same view; the second save would undo the first."""
    _, fake, client = rig(tmp_path)
    first = client.put("/api/settings/models", headers=HEADERS, json=save(BODY))
    assert first.status_code == 200
    stale = {**BODY, "triage": [entry("anthropic", "claude-opus-5")]}
    r = client.put("/api/settings/models", headers=HEADERS, json=save(stale, revision=""))
    assert r.status_code == 409
    assert "reload" in r.json()["detail"]
    r = client.delete("/api/settings/models", headers=HEADERS, params={"revision": ""})
    assert r.status_code == 409
    assert client.get("/api/settings/models", headers=HEADERS).json()["models"] == BODY
    assert [e[0] for e in fake.appended] == ["model-settings-set"]


def test_an_unreadable_file_still_shows_the_page_and_says_why(tmp_path):
    _, _, client = rig(tmp_path)
    (state_dir(tmp_path) / model_settings.FILE).mkdir(parents=True)
    r = client.get("/api/settings/models", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["source"] == "targets.yaml"
    assert "unreadable" in r.json()["error"]
    # A reset from that view is let through and says why it cannot write.
    r = client.delete("/api/settings/models", headers=HEADERS,
                      params={"revision": r.json()["revision"]})
    assert r.status_code == 500
    assert "cannot write the model settings" in r.json()["detail"]



def test_the_dispatcher_parks_a_launch_picked_on_a_provider_no_entry_names(tmp_path, monkeypatch):
    """The same rule the console refuses a save on (models.stranded): a
    targets.yaml edit can still strand a task, and the dispatcher says so
    instead of waiting on a provider whose usage it never reads. Here the
    implement fix round is done and review runs again on its openai pick."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace(cfg(tmp_path), models=tracks_policy())   # every entry is anthropic
    wt = make_saved_task(c, issue=42, stage=Stage.IMPLEMENT, ticket_count=1,
                         picks={"implement": "anthropic/claude-sonnet-5",
                                "review": "openai/gpt-luna@high"})
    (wt / ".agent" / "stage.json").write_text(json.dumps({"stage": "implement", "status": "done"}))

    main._drive_task(c, deps(sess=FakeSessions(alive={42})), c.targets[0],
                     load(c.state_dir, "portfolio_eval", 42), ANTHROPIC_ONLY, tuple)

    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.park, t.park_reason) == (PARK_HUMAN, WHY_CONFIG)
    assert "its review stage runs on openai, which no model entry names" in t.park_note


def test_a_wake_picked_on_a_provider_no_entry_names_parks_again(tmp_path):
    c = replace(cfg(tmp_path), models=tracks_policy())   # every entry is anthropic
    make_saved_task(c, issue=42, stage=Stage.IMPLEMENT, park=PARK_WAKE,
                    picks={"implement": "openai/gpt-luna@high"})

    main._resume_woken(c, deps(), ANTHROPIC_ONLY, tuple)

    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.park, t.park_reason) == (PARK_HUMAN, WHY_CONFIG)
    assert "its implement stage runs on openai" in t.park_note


def test_a_done_stage_on_a_dropped_provider_still_moves_on(tmp_path, monkeypatch):
    """The provider rule judges the launch about to happen, not the stage that
    just ended: implement ran on openai and is done, and review runs on a
    configured provider."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace(cfg(tmp_path), models=tracks_policy())   # every entry is anthropic
    wt = make_saved_task(c, issue=42, stage=Stage.IMPLEMENT, ticket_count=1,
                         picks={"implement": "openai/gpt-luna@high"})
    (wt / ".agent" / "stage.json").write_text(json.dumps({"stage": "implement", "status": "done"}))
    sess = FakeSessions(alive={42})

    main.run_pass(c, deps(sess=sess))

    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.park, t.stage) == ("", Stage.REVIEW)
    assert [s[:2] for s in sess.spawned] == [(42, "review")]


def test_the_dispatcher_warns_when_the_stored_block_is_rejected(tmp_path, capsys):
    store(tmp_path, None)
    boot, cfg = main._load(str(write_config(tmp_path)))
    assert cfg.models == boot.models
    assert "console model settings not applied" in capsys.readouterr().err
