import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dispatcher.budget as budget


def oauth_response(util=50, mins=90):
    resets = datetime.now(timezone.utc) + timedelta(minutes=mins)
    return {"five_hour": {"utilization": util, "resets_at": resets.isoformat()}}


def creds(tmp_path: Path) -> Path:
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123"}}))
    return p


def test_oauth_happy_path(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["url"], seen["headers"] = url, headers
        return oauth_response(util=42, mins=60)

    monkeypatch.setattr(budget, "_http_get_json", fake_get)
    u = budget.fetch_usage(tmp_path, credentials_path=creds(tmp_path))
    assert u.source == "oauth"
    assert abs(u.utilization - 0.42) < 1e-9
    assert 59 <= u.minutes_to_reset <= 61
    assert seen["headers"]["User-Agent"].startswith("claude-code/")
    assert seen["headers"]["Authorization"] == "Bearer tok-123"


def test_cache_respects_min_poll_interval(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, headers):
        calls.append(1)
        return oauth_response()

    monkeypatch.setattr(budget, "_http_get_json", fake_get)
    t = [1000.0]
    now = lambda: t[0]
    cp = creds(tmp_path)
    budget.fetch_usage(tmp_path, credentials_path=cp, now=now)
    t[0] += 60  # only 60s later — must serve the cache
    budget.fetch_usage(tmp_path, credentials_path=cp, now=now)
    assert len(calls) == 1
    t[0] += 200  # past 180s — fetches again
    budget.fetch_usage(tmp_path, credentials_path=cp, now=now)
    assert len(calls) == 2


def test_falls_back_to_ccusage(tmp_path, monkeypatch):
    def boom(url, headers):
        raise budget.UsageFetchError("429")

    monkeypatch.setattr(budget, "_http_get_json", boom)
    monkeypatch.setattr(
        budget, "_ccusage_json",
        lambda: {"blocks": [{"isActive": True, "projection": {"remainingMinutes": 45},
                             "tokenCounts": {}, "usageLimitResetTime": None,
                             "percentUsed": 62.0}]},
    )
    u = budget.fetch_usage(tmp_path, credentials_path=creds(tmp_path))
    assert u.source == "ccusage"
    assert abs(u.utilization - 0.62) < 1e-9
    assert u.minutes_to_reset == 45


def test_both_dark(tmp_path, monkeypatch):
    def boom(url, headers):
        raise budget.UsageFetchError("timeout")

    monkeypatch.setattr(budget, "_http_get_json", boom)
    monkeypatch.setattr(budget, "_ccusage_json", lambda: None)
    u = budget.fetch_usage(tmp_path, credentials_path=creds(tmp_path))
    assert u.source == "unavailable"


def test_missing_credentials_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "_ccusage_json", lambda: None)
    u = budget.fetch_usage(tmp_path, credentials_path=tmp_path / "nope.json")
    assert u.source == "unavailable"


def test_default_prefers_claude_home_store(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(budget, "_http_get_json", fake_get)
    home_store = tmp_path / "claude-home" / ".credentials.json"
    home_store.parent.mkdir()
    home_store.write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok-home"}}))
    u = budget.fetch_usage(tmp_path)
    assert u.source == "oauth"
    assert seen["headers"]["Authorization"] == "Bearer tok-home"


def test_default_falls_back_to_host_store(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(budget, "_http_get_json", fake_get)
    host_store = tmp_path / "host-credentials.json"
    host_store.write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok-host"}}))
    monkeypatch.setattr(budget, "HOST_CREDENTIALS", str(host_store),
                        raising=False)
    u = budget.fetch_usage(tmp_path)  # no claude-home store in state_dir
    assert u.source == "oauth"
    assert seen["headers"]["Authorization"] == "Bearer tok-host"


def test_env_token_preferred_over_credentials_store(tmp_path, monkeypatch):
    # The long-lived setup-token (claude-token.env, injected by the unit's
    # EnvironmentFile) outlives the claude-home store, which lapses ~8h
    # after the last refresh once the fleet stops renewing it.
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(budget, "_http_get_json", fake_get)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "long-lived-tok")
    u = budget.fetch_usage(tmp_path, credentials_path=creds(tmp_path))
    assert u.source == "oauth"
    assert seen["headers"]["Authorization"] == "Bearer long-lived-tok"


def test_env_token_suffices_without_credentials_store(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "_http_get_json",
                        lambda url, headers: oauth_response())
    monkeypatch.setattr(budget, "HOST_CREDENTIALS",
                        str(tmp_path / "absent.json"), raising=True)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "long-lived-tok")
    u = budget.fetch_usage(tmp_path)
    assert u.source == "oauth"
