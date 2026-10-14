"""The console's model settings: a `models:` block stored in the state dir
that replaces the targets.yaml one for the dispatcher and the console."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from dispatcher import model_settings
from dispatcher.config import load_config
from dispatcher.models import parse_policy, policy_to_raw
from dispatcher.state import Stage
from tests.test_config import SAMPLE
from tests.webfakes import HEADERS, FakeSources, make_task
from web.app import create_app

EXAMPLE = Path(__file__).resolve().parent.parent / "targets.example.yaml"
BLOCK = {"triage": ["claude-sonnet-5"], "untracked": "fast",
         "tracks": {"fast": {"when": "Small work.", "spec": ["claude-sonnet-5@low"],
                             "plan": ["claude-sonnet-5"], "implement": ["openai/gpt-luna@high"],
                             "review": ["claude-opus-5"]}}}


def write_config(tmp_path, extra=""):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE + extra)
    return p


def state_dir(tmp_path):
    return tmp_path / "state"   # conftest's AGENT_OPS_STATE_DIR


def test_policy_to_raw_round_trips_the_example_policy():
    policy = parse_policy(yaml.safe_load(EXAMPLE.read_text())["models"])
    assert parse_policy(policy_to_raw(policy)) == policy


def test_a_stored_block_replaces_the_targets_yaml_one(tmp_path):
    model_settings.save(state_dir(tmp_path), BLOCK, actor="jesdi",
                        now=datetime.now(timezone.utc))
    cfg = load_config(write_config(tmp_path))
    assert list(cfg.models.tracks) == ["fast"]
    assert list(cfg.file_models.tracks) == ["standard"]
    assert cfg.models_override_error == ""


def test_a_bad_stored_block_falls_back_to_targets_yaml_and_says_why(tmp_path):
    d = state_dir(tmp_path)
    d.mkdir(parents=True)
    (d / model_settings.FILE).write_text(json.dumps({"models": {**BLOCK, "untracked": "nope"}}))
    cfg = load_config(write_config(tmp_path))
    assert cfg.models == cfg.file_models
    assert "untracked" in cfg.models_override_error


def test_a_stored_block_must_keep_every_session_week_share_provider(tmp_path):
    only_anthropic = {**BLOCK, "tracks": {"fast": {**BLOCK["tracks"]["fast"],
                                                   "implement": ["claude-sonnet-5"]}}}
    d = state_dir(tmp_path)
    d.mkdir(parents=True)
    (d / model_settings.FILE).write_text(json.dumps({"models": only_anthropic}))
    extra = yaml.safe_dump({"models": BLOCK, "session_week_share": {"openai": 0.1}})
    cfg = load_config(write_config(tmp_path, extra))
    assert "session_week_share" in cfg.models_override_error


def rig(tmp_path, tasks=()):
    cfg = load_config(write_config(tmp_path))
    fake = FakeSources()
    fake.tasks_list = list(tasks)
    return cfg, fake, TestClient(create_app(cfg, fake))


def test_the_console_stores_shows_and_resets_the_block(tmp_path):
    cfg, fake, client = rig(tmp_path)
    view = client.get("/api/settings/models", headers=HEADERS).json()
    assert view["source"] == "targets.yaml"
    assert view["efforts"]["openai"][0] == "minimal"

    r = client.put("/api/settings/models", headers=HEADERS, json=BLOCK)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "console"
    assert r.json()["set_by"] == "jesdi@github"
    assert list(r.json()["models"]["tracks"]) == ["fast"]
    # The console's own views follow at once; the next pass reads the file.
    usage = client.get("/api/usage", headers=HEADERS).json()
    assert usage["gate"]["model"] == "anthropic/claude-sonnet-5"
    assert usage["priority"]["options"] == ["auto", "anthropic", "openai"]
    assert list(load_config(write_config(tmp_path)).models.tracks) == ["fast"]
    assert [e[0] for e in fake.appended] == ["model-settings-set"]

    r = client.delete("/api/settings/models", headers=HEADERS)
    assert r.json()["source"] == "targets.yaml"
    assert not (state_dir(tmp_path) / model_settings.FILE).exists()
    assert list(r.json()["models"]["tracks"]) == ["standard"]


def test_an_invalid_block_is_refused_and_nothing_is_stored(tmp_path):
    _, fake, client = rig(tmp_path)
    bad = {**BLOCK, "tracks": {"fast": {**BLOCK["tracks"]["fast"], "plan": ["claude-x@huge"]}}}
    r = client.put("/api/settings/models", headers=HEADERS, json=bad)
    assert r.status_code == 422
    assert "effort" in r.json()["detail"]
    assert not (state_dir(tmp_path) / model_settings.FILE).exists()
    assert fake.appended == []


def test_a_block_that_drops_a_track_in_use_is_refused(tmp_path):
    busy = make_task(issue=9, target="portfolio_eval", track="standard")
    done = make_task(issue=10, target="portfolio_eval", track="standard", stage=Stage.DONE)
    _, _, client = rig(tmp_path, [busy, done])
    r = client.put("/api/settings/models", headers=HEADERS, json=BLOCK)
    assert r.status_code == 422
    assert r.json()["detail"].endswith("portfolio_eval#9 (standard)")
    assert not (state_dir(tmp_path) / model_settings.FILE).exists()


def test_load_says_why_a_stored_file_is_unusable(tmp_path):
    path = tmp_path / model_settings.FILE
    assert model_settings.load(tmp_path) is None
    for text, why in (("{", "not JSON"), ("[]", "no models"), ('{"models": {}}', "no models")):
        path.write_text(text)
        with pytest.raises(ValueError, match=why):
            model_settings.load(tmp_path)
    path.unlink()
    path.mkdir()   # a directory: read_text fails with an OSError
    with pytest.raises(ValueError, match="unreadable"):
        model_settings.load(tmp_path)
