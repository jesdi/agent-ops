"""The console's model settings: a `models:` block stored in the state dir
that replaces the targets.yaml one for the dispatcher and the console."""
import json
from datetime import datetime, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from dispatcher import model_settings
from dispatcher.config import apply_stored_models, load_config
from dispatcher.convergence import pass_lock
from dispatcher.models import entry_text
from dispatcher.state import Stage
from tests.test_config import SAMPLE
from tests.webfakes import HEADERS, FakeSources, make_task
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


def test_a_stored_block_replaces_the_targets_yaml_one(tmp_path):
    model_settings.save(state_dir(tmp_path), BLOCK, actor="jesdi",
                        now=datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))
    cfg = load_config(write_config(tmp_path))
    assert list(cfg.models.tracks) == ["fast"]
    assert list(cfg.file_models.tracks) == ["standard"]
    assert cfg.models_origin == model_settings.Stored("jesdi", "2026-10-14T09:00:00+00:00")


@pytest.mark.parametrize("models, why", [
    ({**BLOCK, "untracked": "nope"}, "untracked"),
    ({}, "must not be empty"),
])
def test_a_bad_stored_block_falls_back_to_targets_yaml_and_says_why(tmp_path, models, why):
    store(tmp_path, models)
    cfg = load_config(write_config(tmp_path))
    assert cfg.models == cfg.file_models
    assert isinstance(cfg.models_origin, model_settings.Rejected)
    assert why in cfg.models_origin.reason


def test_a_stored_block_must_keep_every_session_week_share_provider(tmp_path):
    store(tmp_path, {**BLOCK, "tracks": {"fast": {**BLOCK["tracks"]["fast"],
                                                  "implement": ["claude-sonnet-5"]}}})
    extra = yaml.safe_dump({"models": BLOCK, "session_week_share": {"openai": 0.1}})
    cfg = load_config(write_config(tmp_path, extra))
    assert "session_week_share" in cfg.models_origin.reason


def test_apply_stored_models_follows_the_file_as_it_is_now(tmp_path):
    """The dispatcher applies it again inside the pass lock: a block stored
    after load_config is the one the pass runs."""
    cfg = load_config(write_config(tmp_path))
    store(tmp_path, BLOCK)
    assert list(apply_stored_models(cfg).models.tracks) == ["fast"]
    (state_dir(tmp_path) / model_settings.FILE).unlink()
    assert apply_stored_models(cfg).models == cfg.file_models


def test_load_says_why_a_stored_file_is_unusable(tmp_path):
    path = tmp_path / model_settings.FILE
    assert model_settings.load(tmp_path) is None
    for text, why in (("{", "not JSON"), ("[]", "no models"), ("{}", "no models")):
        path.write_text(text)
        with pytest.raises(ValueError, match=why):
            model_settings.load(tmp_path)
    path.unlink()
    path.mkdir()   # a directory: read_text fails with an OSError
    with pytest.raises(ValueError, match="unreadable"):
        model_settings.load(tmp_path)


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
    assert r.json()["revision"] == model_settings.revision(state_dir(tmp_path)) != ""
    assert r.json()["source"] == "console"
    assert r.json()["set_by"] == "jesdi@github"
    assert r.json()["models"] == BODY
    assert r.json()["known_models"] == {
        "anthropic": ["claude-opus-5", "claude-sonnet-5"], "openai": ["gpt-luna"]}
    # The console's own views follow at once; the next pass reads the file.
    usage = client.get("/api/usage", headers=HEADERS).json()
    assert usage["gate"]["model"] == "anthropic/claude-sonnet-5"
    assert usage["priority"]["options"] == ["auto", "anthropic", "openai"]
    assert list(load_config(write_config(tmp_path)).models.tracks) == ["fast"]
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
    assert r.json()["detail"].endswith("portfolio_eval#9 (standard)")
    assert not (state_dir(tmp_path) / model_settings.FILE).exists()


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


def test_entry_text_refuses_a_separator_inside_a_part():
    assert entry_text("openai", "gpt-luna", "high", "x") == "openai/gpt-luna@high"
    with pytest.raises(ValueError, match="must not contain"):
        entry_text("anthropic", "claude@x", "", "x")
