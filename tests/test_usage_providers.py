import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dispatcher import usage_providers as up
from dispatcher.config import Config, Repository
from dispatcher.usage import ProviderUsage, WindowKind
from tests.usagefakes import FakeUsage, session_usage

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "anthropic-usage.json").read_text())


def oauth_response(util=50, mins=90):
    resets = (datetime.now(timezone.utc) + timedelta(minutes=mins)).isoformat()
    return {"limits": [
        {"kind": "session", "percent": util, "resets_at": resets, "scope": None},
        {"kind": "weekly_all", "percent": 10, "resets_at": resets, "scope": None}]}


def creds(tmp_path: Path) -> Path:
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123"}}))
    return p


def cfg(tmp_path, models=None):
    return Config(state_dir=str(tmp_path), capacity=1, session_memory="1g",
                  session_cpus="2", repositories=[],
                  **({"models": models} if models else {}))


def one_track_policy(*entries: str):
    from dispatcher.models import parse_policy
    stage = list(entries)
    return parse_policy({"triage": ["anthropic/claude-sonnet-4-6"], "untracked": "t", "tracks": {
        "t": {"when": "w", "spec": stage, "plan": stage,
              "implement": stage, "review": stage}}})


# ---- new tests from brief -----------------------------------------------

def test_oauth_happy_path_parses_limits(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["url"], seen["headers"] = url, headers
        return FIXTURE

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.provider == "anthropic" and u.source == "oauth"
    assert [(w.kind, w.scope) for w in u.windows] == [
        ("session", None), ("weekly", None), ("weekly", "Fable")]
    assert seen["headers"]["User-Agent"].startswith("claude-code/")
    assert seen["headers"]["Authorization"] == "Bearer tok-123"


def test_cache_is_per_provider_and_respects_min_poll(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: calls.append(1) or oauth_response())
    t = [1000.0]
    now = lambda: t[0]
    adapters = {"anthropic": up.AnthropicUsage(credentials_path=creds(tmp_path))}
    up.fetch_provider("anthropic", tmp_path, now=now, adapters=adapters)
    assert (tmp_path / "usage" / "anthropic.json").exists()
    t[0] += 60
    up.fetch_provider("anthropic", tmp_path, now=now, adapters=adapters)
    assert len(calls) == 1
    t[0] += 200
    up.fetch_provider("anthropic", tmp_path, now=now, adapters=adapters)
    assert len(calls) == 2


def test_live_token_preserves_usage_in_every_window(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: FIXTURE)
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "oauth"
    assert [(w.kind, w.scope, w.used) for w in u.windows] == [
        ("session", None, 0.05), ("weekly", None, 0.13), ("weekly", "Fable", 0.23)]


def test_cache_clock_step_back_triggers_refetch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: calls.append(1) or oauth_response())
    t = [1000.0]
    now = lambda: t[0]
    adapters = {"anthropic": up.AnthropicUsage(credentials_path=creds(tmp_path))}
    up.fetch_provider("anthropic", tmp_path, now=now, adapters=adapters)
    t[0] = 500.0  # clock stepped back
    up.fetch_provider("anthropic", tmp_path, now=now, adapters=adapters)
    assert len(calls) == 2


@pytest.fixture
def ccusage_on_path(tmp_path, monkeypatch):
    """A working ccusage first on PATH: an unavailable reading must stay
    unavailable whatever local tool would answer."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ccusage"
    marker = tmp_path / "ccusage-invoked"
    monkeypatch.setenv("CCUSAGE_MARKER", str(marker))
    fake.write_text("#!/bin/sh\n"
                    'touch "$CCUSAGE_MARKER"\n'
                    'echo \'{"blocks": [{"isActive": true, "projection": {"remainingMinutes": 45}, '
                    '"percentUsed": 62.0}]}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return marker


def boom(url, headers):
    raise up.UsageFetchError("dark")


def test_unavailable_is_never_cached(tmp_path, monkeypatch, ccusage_on_path):
    monkeypatch.setattr(up, "_http_get_json", boom)
    adapters = {"anthropic": up.AnthropicUsage(credentials_path=creds(tmp_path))}
    u = up.fetch_provider("anthropic", tmp_path, adapters=adapters)
    assert u.source == "unavailable"
    assert not (tmp_path / "usage" / "anthropic.json").exists()


def test_a_429_spaces_the_next_oauth_request(tmp_path, monkeypatch):
    """#1: a rate-limited usage endpoint is not called again on the next pass."""
    import email.message
    import urllib.error
    requests = []

    def rate_limited(req, timeout):
        requests.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     email.message.Message(), None)

    monkeypatch.setattr(up.urllib.request, "urlopen", rate_limited)
    t = [1000.0]
    adapters = {"anthropic": up.AnthropicUsage(credentials_path=creds(tmp_path))}
    first = up.fetch_provider("anthropic", tmp_path, now=lambda: t[0], adapters=adapters)
    t[0] += 5
    second = up.fetch_provider("anthropic", tmp_path, now=lambda: t[0], adapters=adapters)
    assert (first.source, second.source) == ("unavailable", "unavailable")
    assert len(requests) == 1


def test_a_dead_token_is_unavailable_even_if_ccusage_would_answer(tmp_path, monkeypatch, ccusage_on_path):
    """#75: ccusage reads local JSONL logs and proves nothing about the OAuth
    token the fleet needs. A dead token must read as unavailable, never as a
    healthy ccusage session window: the auth-dark detector and the spawn gate
    only act on `unavailable`."""
    monkeypatch.setattr(up, "_http_get_json", boom)
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "unavailable" and u.windows == ()


def test_a_dead_token_never_runs_ccusage(tmp_path, monkeypatch, ccusage_on_path):
    monkeypatch.setattr(up, "_http_get_json", boom)
    up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert not ccusage_on_path.exists()


def test_both_dark_is_unavailable(tmp_path, monkeypatch, ccusage_on_path):
    monkeypatch.setattr(up, "_http_get_json", boom)
    assert up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path).source == "unavailable"


def test_missing_credentials_is_unavailable(tmp_path, ccusage_on_path):
    assert up.AnthropicUsage(credentials_path=tmp_path / "nope.json").fetch(tmp_path).source == "unavailable"


def test_fetch_all_covers_only_referenced_providers(tmp_path):
    fake = FakeUsage()
    c = cfg(tmp_path, models=one_track_policy("openai/m"))
    out = up.fetch_all(c, adapters={"openai": fake, "anthropic": FakeUsage()})
    assert set(out) == {"anthropic", "openai"} and fake.calls == 1


def test_fetch_all_reports_missing_adapter_as_unavailable(tmp_path):
    c = cfg(tmp_path, models=one_track_policy(
        "openai/gpt-sol", "anthropic/claude-sonnet-4-6"))
    out = up.fetch_all(c, adapters={"anthropic": FakeUsage()})
    assert out["openai"].source == "unavailable"
    assert out["anthropic"].source == "oauth"


# ---- ported from test_budget_fetch.py ------------------------------------

def test_default_prefers_claude_home_store(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    home_store = tmp_path / "claude-home" / ".credentials.json"
    home_store.parent.mkdir()
    home_store.write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok-home"}}))
    u = up.AnthropicUsage().fetch(tmp_path)
    assert u.source == "oauth"
    assert seen["headers"]["Authorization"] == "Bearer tok-home"


def test_refused_claude_home_token_falls_through_to_host_store(tmp_path, monkeypatch):
    """The box on 2026-09-23: claude-home held a token the endpoint 429s
    while the host login was live — the gate must read usage via the host."""
    tried = []

    def fake_get(url, headers):
        tried.append(headers["Authorization"])
        if headers["Authorization"] == "Bearer tok-home":
            raise up.UsageFetchError("HTTP Error 429: Too Many Requests")
        return oauth_response()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    home_store = tmp_path / "claude-home" / ".credentials.json"
    home_store.parent.mkdir()
    home_store.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-home"}}))
    host_store = tmp_path / "host-credentials.json"
    host_store.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-host"}}))
    monkeypatch.setattr(up, "HOST_CREDENTIALS", str(host_store))
    u = up.AnthropicUsage().fetch(tmp_path)
    assert u.source == "oauth"
    assert tried == ["Bearer tok-home", "Bearer tok-host"]


def test_default_falls_back_to_host_store(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    host_store = tmp_path / "host-credentials.json"
    host_store.write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok-host"}}))
    monkeypatch.setattr(up, "HOST_CREDENTIALS", str(host_store),
                        raising=False)
    u = up.AnthropicUsage().fetch(tmp_path)  # no claude-home store in state_dir
    assert u.source == "oauth"
    assert seen["headers"]["Authorization"] == "Bearer tok-host"


def test_env_token_preferred_over_credentials_store(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "long-lived-tok")
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "oauth"
    assert seen["headers"]["Authorization"] == "Bearer long-lived-tok"


def test_env_token_suffices_without_credentials_store(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json",
                        lambda url, headers: oauth_response())
    monkeypatch.setattr(up, "HOST_CREDENTIALS",
                        str(tmp_path / "absent.json"), raising=True)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "long-lived-tok")
    u = up.AnthropicUsage().fetch(tmp_path)
    assert u.source == "oauth"


# ---- fail closed on unreadable readings ------------------------------------

@pytest.mark.parametrize("payload", [{"limits": []}, {}])
def test_an_empty_oauth_reading_is_not_oauth(tmp_path, monkeypatch, ccusage_on_path, payload):
    """A 200 with zero windows would admit every model; it fails closed like
    a rejected token: next token, then unavailable."""
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: payload)
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "unavailable" and u.windows == ()


def test_an_empty_oauth_reading_tries_the_next_token(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: (
        {"limits": []} if headers["Authorization"] == "Bearer env-tok" else FIXTURE))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok")
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "oauth" and len(u.windows) == 3




class RaisingAdapter:
    name = "boom"

    def fetch(self, state_dir, *, now=time.time):
        raise RuntimeError("adapter bug")


def test_a_raising_adapter_reports_unavailable_and_caches_nothing(tmp_path):
    u = up.fetch_provider("boom", tmp_path, now=lambda: 42.0,
                          adapters={"boom": RaisingAdapter()})
    assert (u.provider, u.source, u.fetched_at) == ("boom", "unavailable", 42.0)
    assert not up.cache_path(tmp_path, "boom").exists()


@pytest.mark.parametrize("payload", [
    {"limits": {"kind": "session"}},
    {"limits": ["session"]},
    {"limits": [dict(FIXTURE["limits"][0], resets_at=1757764799)]},
    ["not", "an", "object"],
])
def test_a_malformed_anthropic_payload_is_unavailable_not_a_raise(tmp_path, monkeypatch, ccusage_on_path, payload):
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: payload)
    adapters = {"anthropic": up.AnthropicUsage(credentials_path=creds(tmp_path))}
    assert up.fetch_provider("anthropic", tmp_path, adapters=adapters).source == "unavailable"




# ---- Anthropic payload parser ----------------------------------------------

def test_parse_reads_three_windows_from_limits():
    ws = up.parse_anthropic(FIXTURE)
    assert [(w.kind, w.scope) for w in ws] == [
        ("session", None), ("weekly", None), ("weekly", "Fable")]
    assert [w.used for w in ws] == [0.05, 0.13, 0.23]
    assert [w.length for w in ws] == [timedelta(hours=5), timedelta(days=7), timedelta(days=7)]
    assert ws[0].resets_at == datetime(2026, 9, 13, 11, 59, 59, 776452, tzinfo=timezone.utc)
    assert ws[2].resets_at.tzinfo is not None


def test_parse_falls_back_to_named_windows_when_limits_absent():
    payload = {k: v for k, v in FIXTURE.items() if k != "limits"}
    ws = up.parse_anthropic(payload)
    assert [(w.kind, w.scope, w.used) for w in ws] == [
        ("session", None, 0.05), ("weekly", None, 0.13)]


def test_parse_locked_or_full_window_counts_as_fully_used():
    limits = [dict(FIXTURE["limits"][0], locked_reason="limit_reached"),
              dict(FIXTURE["limits"][1], percent=130)]
    ws = up.parse_anthropic({"limits": limits})
    assert [w.used for w in ws] == [1.0, 1.0]


def test_parse_skips_unknown_kinds_and_entries_without_reset():
    limits = [dict(FIXTURE["limits"][0], kind="monthly_mystery"),
              dict(FIXTURE["limits"][1], resets_at=None),
              FIXTURE["limits"][2]]
    ws = up.parse_anthropic({"limits": limits})
    assert [(w.kind, w.scope) for w in ws] == [("weekly", "Fable")]


def test_parse_skips_non_object_entries_and_reads_a_malformed_scope_as_unscoped():
    """A scope of unexpected shape must not exempt every other model from the
    window: it counts as unscoped, the fail-closed reading."""
    limits = ["session", None, 7, dict(FIXTURE["limits"][2], scope="Fable")]
    ws = up.parse_anthropic({"limits": limits})
    assert [(w.kind, w.scope, w.used) for w in ws] == [("weekly", None, 0.23)]


def test_parse_locked_without_percent_via_limits():
    # null percent + locked_reason in limits[] must return 1.0, not skip
    limits = [dict(FIXTURE["limits"][0], percent=None, locked_reason="limit_reached")]
    ws = up.parse_anthropic({"limits": limits})
    assert len(ws) == 1
    assert ws[0].used == 1.0


def test_parse_locked_without_percent_via_fallback():
    # null utilization + locked_reason in five_hour fallback must return 1.0, not skip
    payload = {
        "five_hour": {"utilization": None, "locked_reason": "limit_reached",
                      "resets_at": "2026-09-13T11:59:59.776452Z"},
        "seven_day": {"utilization": 10.0, "resets_at": "2026-09-20T00:00:00Z"}
    }
    ws = up.parse_anthropic(payload)
    assert len(ws) == 2
    assert ws[0].used == 1.0
    assert ws[1].used == 0.1


def test_json_round_trip():
    u = ProviderUsage(provider="anthropic", source="oauth", fetched_at=1000.0,
                      windows=up.parse_anthropic(FIXTURE))
    assert up.usage_from_json(json.loads(json.dumps(up.usage_to_json(u)))) == u


def test_cache_with_an_unreadable_window_kind_refetches(tmp_path):
    fake = FakeUsage()
    cached = up.usage_to_json(session_usage(provider="fake"))
    cached["windows"][0]["kind"] = "monthly"
    up.cache_path(tmp_path, "fake").parent.mkdir(parents=True)
    up.cache_path(tmp_path, "fake").write_text(json.dumps({"fetched_at": 1000.0, "usage": cached}))
    up.fetch_provider("fake", tmp_path, now=lambda: 1010.0, adapters={"fake": fake})
    assert fake.calls == 1


def test_one_clock_reading_serves_the_whole_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: oauth_response())
    ticks = iter(range(1000, 2000))
    adapters = {"anthropic": up.AnthropicUsage(credentials_path=creds(tmp_path))}
    u = up.fetch_provider("anthropic", tmp_path, now=lambda: float(next(ticks)),
                          adapters=adapters)
    cached = json.loads(up.cache_path(tmp_path, "anthropic").read_text())
    assert cached["fetched_at"] == u.fetched_at == cached["usage"]["fetched_at"]


def test_the_cache_is_written_aside_and_renamed_over(tmp_path, monkeypatch):
    """The web process and the dispatcher both write the cache; a reader must
    never see a half-written file."""
    replaced = []
    real_replace = up.os.replace

    def spy(src, dst):
        replaced.append((Path(src).parent, Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(up.os, "replace", spy)
    up.fetch_provider("fake", tmp_path, adapters={"fake": FakeUsage()})
    cp = up.cache_path(tmp_path, "fake")
    assert replaced == [(cp.parent, cp)]
    assert [p.name for p in cp.parent.iterdir()] == ["fake.json"]


# ---- the cache across units ------------------------------------------------

def test_the_cache_file_is_readable_by_other_units(tmp_path):
    """mkstemp creates 0600; the web and dispatcher units may run as
    different users, so the renamed cache file must be 0644."""
    up.fetch_provider("fake", tmp_path, adapters={"fake": FakeUsage()})
    assert up.cache_path(tmp_path, "fake").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_an_unreadable_cache_file_is_a_miss(tmp_path, monkeypatch, error):
    fake = FakeUsage()
    up.fetch_provider("fake", tmp_path, now=lambda: 1000.0, adapters={"fake": fake})
    cp = up.cache_path(tmp_path, "fake")
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == cp:
            raise error("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    u = up.fetch_provider("fake", tmp_path, now=lambda: 1010.0, adapters={"fake": fake})
    assert fake.calls == 2 and u == fake.result


def test_an_empty_cached_reading_is_a_miss(tmp_path):
    """A cache written before readings failed closed may hold an oauth
    reading with no windows, which would admit every model for 180s."""
    fake = FakeUsage()
    empty = ProviderUsage(provider="fake", source="oauth", fetched_at=1000.0, windows=())
    up.cache_path(tmp_path, "fake").parent.mkdir(parents=True)
    up.cache_path(tmp_path, "fake").write_text(
        json.dumps({"fetched_at": 1000.0, "usage": up.usage_to_json(empty)}))
    u = up.fetch_provider("fake", tmp_path, now=lambda: 1010.0, adapters={"fake": fake})
    assert fake.calls == 1 and u == fake.result


# ---- OpenAI (Codex) -------------------------------------------------------

OPENAI_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "openai-usage.json").read_text())


def codex_auth(tmp_path: Path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps(
        {"tokens": {"access_token": "cx-tok", "account_id": "acct-1"}}))
    return home / "auth.json"


def openai_window(seconds, used=10, reset_at=1790719147):
    return {"used_percent": used, "limit_window_seconds": seconds, "reset_at": reset_at}


def test_openai_reads_the_captured_payload(tmp_path, monkeypatch):
    codex_auth(tmp_path)
    seen = {}

    def fake_get(url, headers):
        seen["url"], seen["headers"] = url, headers
        return OPENAI_FIXTURE

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    u = up.OpenAIUsage().fetch(tmp_path)
    assert u.provider == "openai" and u.source == "oauth"
    assert seen["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert seen["headers"]["Authorization"] == "Bearer cx-tok"
    assert seen["headers"]["ChatGPT-Account-Id"] == "acct-1"
    [w] = u.windows
    assert (w.kind, w.scope, w.used) == (WindowKind.WEEKLY, None, 0.02)
    assert w.resets_at == datetime.fromtimestamp(1790719147, timezone.utc)


def test_openai_kind_comes_from_window_length_not_slot():
    windows = up.parse_openai({"rate_limit": {
        "primary_window": openai_window(5 * 3600, used=40),
        "secondary_window": openai_window(604800, used=7)}})
    assert [(w.kind, w.used) for w in windows] == [
        (WindowKind.SESSION, 0.4), (WindowKind.WEEKLY, 0.07)]


@pytest.mark.parametrize("flags", [{"limit_reached": True}, {"allowed": False}])
def test_openai_limit_reached_counts_as_fully_used(flags):
    windows = up.parse_openai({"rate_limit": {
        **flags, "primary_window": openai_window(604800, used=30), "secondary_window": None}})
    assert [w.used for w in windows] == [1.0]


@pytest.mark.parametrize("payload", [
    {}, {"rate_limit": None}, {"rate_limit": {"primary_window": None, "secondary_window": None}},
    {"rate_limit": {"primary_window": {"used_percent": 5}}}, {"rate_limit": "x"}])
def test_openai_unreadable_payload_is_unavailable(tmp_path, monkeypatch, payload):
    codex_auth(tmp_path)
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: payload)
    assert up.OpenAIUsage().fetch(tmp_path).source == "unavailable"


def test_openai_http_error_is_unavailable(tmp_path, monkeypatch):
    codex_auth(tmp_path)
    monkeypatch.setattr(up, "_http_get_json", boom)
    assert up.OpenAIUsage().fetch(tmp_path).source == "unavailable"


@pytest.mark.parametrize("broken_window", [
    {"used_percent": 100, "limit_window_seconds": 604800},
    {"reset_at": 1790719147, "limit_window_seconds": 604800},
    "unreadable",
])
def test_openai_malformed_window_invalidates_an_otherwise_valid_reading(
        tmp_path, monkeypatch, broken_window):
    codex_auth(tmp_path)
    payload = {"rate_limit": {
        "primary_window": openai_window(5 * 3600, used=1),
        "secondary_window": broken_window}}
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: payload)
    usage = up.OpenAIUsage().fetch(tmp_path)
    assert usage.source == "unavailable"
    assert usage.windows == ()


@pytest.mark.parametrize("auth", [None, "not json", '{"tokens": {}}', '{"tokens": null}'])
def test_openai_missing_or_broken_auth_is_unavailable_without_a_request(tmp_path, monkeypatch, auth):
    if auth is not None:
        (tmp_path / "codex-home").mkdir()
        (tmp_path / "codex-home" / "auth.json").write_text(auth)
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: pytest.fail("requested"))
    assert up.OpenAIUsage().fetch(tmp_path).source == "unavailable"


def test_openai_never_writes_auth_json(tmp_path, monkeypatch):
    auth = codex_auth(tmp_path)
    before = (auth.read_bytes(), auth.stat().st_mtime_ns)
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: OPENAI_FIXTURE)
    up.OpenAIUsage().fetch(tmp_path)
    assert (auth.read_bytes(), auth.stat().st_mtime_ns) == before


def test_openai_adapter_is_registered():
    assert isinstance(up.ADAPTERS["openai"], up.OpenAIUsage)


# ---- #1: a 429 cools down that token, and the read falls through ------------

def token_key(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def marker_file(tmp_path):
    return up.cooldown_path(tmp_path, "anthropic")


def put_marker(tmp_path, content):
    p = marker_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if isinstance(content, str) else json.dumps(content))


def marker(tmp_path):
    return json.loads(marker_file(tmp_path).read_text())


def rate_limited(retry_after=None):
    return up.UsageFetchError("x", status=429, retry_after=retry_after)


def serve(monkeypatch, answers):
    """_http_get_json answering per bearer token: an exception is raised,
    anything else returned. Returns the list of tokens asked, in order."""
    asked = []

    def fake_get(url, headers):
        token = headers["Authorization"].removeprefix("Bearer ")
        asked.append(token)
        answer = answers[token]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    return asked


def watch_ccusage(monkeypatch, result=None):
    calls = []
    monkeypatch.setattr(up, "_ccusage_json", lambda: calls.append(1) or result)
    return calls


SESSION_BLOCK = {"blocks": [
    {"isActive": True, "projection": {"remainingMinutes": 45}, "percentUsed": 62.0}]}


def one_token(tmp_path, monkeypatch, token="tok-a"):
    """`token` as the only candidate: no env token, one credentials file."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    store = tmp_path / "only-credentials.json"
    store.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    return up.AnthropicUsage(credentials_path=store)


def two_stores(tmp_path, monkeypatch, home="tok-home", host="tok-host"):
    """The claude-home store then the host store, as AnthropicUsage() reads them."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    home_store = tmp_path / "claude-home" / ".credentials.json"
    home_store.parent.mkdir()
    home_store.write_text(json.dumps({"claudeAiOauth": {"accessToken": home}}))
    host_store = tmp_path / "host-credentials.json"
    host_store.write_text(json.dumps({"claudeAiOauth": {"accessToken": host}}))
    monkeypatch.setattr(up, "HOST_CREDENTIALS", str(host_store))
    return up.AnthropicUsage()


def http_error(status, headers=None):
    import email.message
    import urllib.error
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("https://x", status, "msg", hdrs, None)


def get_json_raising(monkeypatch, exc):
    def urlopen(req, timeout):
        raise exc
    monkeypatch.setattr(up.urllib.request, "urlopen", urlopen)
    return up._http_get_json("https://x", {})


def test_the_cooldown_constants():
    assert up.RATE_LIMIT_COOLDOWN_SECONDS == 300
    assert up.MAX_COOLDOWN_SECONDS == 3600


def test_the_marker_is_a_different_file_from_the_cache(tmp_path):
    assert up.cooldown_path(tmp_path, "anthropic") == tmp_path / "usage" / "anthropic.cooldown.json"
    assert up.cooldown_path(tmp_path, "anthropic") != up.cache_path(tmp_path, "anthropic")


def test_a_usage_fetch_error_built_from_a_message_has_no_status():
    e = up.UsageFetchError("x")
    assert (e.status, e.retry_after) == (None, None)


def test_http_get_json_keeps_the_status_and_retry_after(monkeypatch):
    with pytest.raises(up.UsageFetchError) as caught:
        get_json_raising(monkeypatch, http_error(429, {"Retry-After": "90"}))
    assert (caught.value.status, caught.value.retry_after) == (429, 90)


def test_retry_after_zero_is_a_valid_value(monkeypatch):
    with pytest.raises(up.UsageFetchError) as caught:
        get_json_raising(monkeypatch, http_error(429, {"Retry-After": "0"}))
    assert (caught.value.status, caught.value.retry_after) == (429, 0)


@pytest.mark.parametrize("headers", [
    {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, {"Retry-After": "-5"},
    {"Retry-After": "abc"}, {"Retry-After": "²"}, {}, None])
def test_an_unusable_retry_after_reads_as_none_but_keeps_the_status(monkeypatch, headers):
    exc = http_error(429, headers or {})
    if headers is None:
        exc.hdrs = None
    with pytest.raises(up.UsageFetchError) as caught:
        get_json_raising(monkeypatch, exc)
    assert (caught.value.status, caught.value.retry_after) == (429, None)


@pytest.mark.parametrize("make", [
    lambda: __import__("urllib.error").error.URLError("down"),
    lambda: TimeoutError("timed out")])
def test_a_transport_failure_carries_no_status(monkeypatch, make):
    with pytest.raises(up.UsageFetchError) as caught:
        get_json_raising(monkeypatch, make())
    assert (caught.value.status, caught.value.retry_after) == (None, None)


def test_a_non_json_body_carries_no_status(monkeypatch):
    class Body:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"<html>"

    monkeypatch.setattr(up.urllib.request, "urlopen", lambda req, timeout: Body())
    with pytest.raises(up.UsageFetchError) as caught:
        up._http_get_json("https://x", {})
    assert (caught.value.status, caught.value.retry_after) == (None, None)


def test_openai_ignores_the_status_and_writes_no_marker(tmp_path, monkeypatch):
    codex_auth(tmp_path)
    requests = []

    def fake_get(url, headers):
        requests.append(url)
        raise rate_limited()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    first = up.OpenAIUsage().fetch(tmp_path, now=lambda: 1000)
    second = up.OpenAIUsage().fetch(tmp_path, now=lambda: 1005)
    assert (first.source, second.source) == ("unavailable", "unavailable")
    assert len(requests) == 2
    assert not up.cooldown_path(tmp_path, "openai").exists()


def test_a_429_writes_a_digest_keyed_marker_and_skips_ccusage(tmp_path, monkeypatch):
    adapter = one_token(tmp_path, monkeypatch, "tok-a")
    serve(monkeypatch, {"tok-a": rate_limited()})
    ccusage = watch_ccusage(monkeypatch, SESSION_BLOCK)
    u = adapter.fetch(tmp_path, now=lambda: 1000)
    assert u.source == "unavailable"
    assert ccusage == []
    assert marker(tmp_path) == {token_key("tok-a"): 1300}
    assert "tok-a" not in marker_file(tmp_path).read_text()


def test_a_429_falls_through_to_the_next_token_and_the_next_pass_skips_it(tmp_path, monkeypatch):
    adapter = two_stores(tmp_path, monkeypatch)
    asked = serve(monkeypatch, {"tok-home": rate_limited(), "tok-host": oauth_response()})
    first = adapter.fetch(tmp_path, now=lambda: 1000)
    assert first.source == "oauth"
    assert asked == ["tok-home", "tok-host"]
    assert marker(tmp_path) == {token_key("tok-home"): 1300}
    asked.clear()
    second = adapter.fetch(tmp_path, now=lambda: 1005)
    assert second.source == "oauth"
    assert asked == ["tok-host"]


def test_every_token_cooling_makes_no_request_and_no_ccusage(tmp_path, monkeypatch):
    adapter = two_stores(tmp_path, monkeypatch)
    put_marker(tmp_path, {token_key("tok-home"): 1300, token_key("tok-host"): 1300})
    asked = serve(monkeypatch, {"tok-home": oauth_response(), "tok-host": oauth_response()})
    ccusage = watch_ccusage(monkeypatch, SESSION_BLOCK)
    u = adapter.fetch(tmp_path, now=lambda: 1000)
    assert u.source == "unavailable"
    assert asked == [] and ccusage == []


def test_a_429_beside_another_failure_still_runs_ccusage(tmp_path, monkeypatch):
    adapter = one_token(tmp_path, monkeypatch, "tok-store")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-env")
    asked = serve(monkeypatch, {"tok-env": rate_limited(),
                                "tok-store": up.UsageFetchError("x", status=403)})
    ccusage = watch_ccusage(monkeypatch, SESSION_BLOCK)
    u = adapter.fetch(tmp_path, now=lambda: 1000)
    assert u.source == "ccusage"
    assert asked == ["tok-env", "tok-store"]
    assert ccusage == [1]
    assert marker(tmp_path) == {token_key("tok-env"): 1300}


def test_no_candidate_token_still_runs_ccusage(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(up, "HOST_CREDENTIALS", str(tmp_path / "absent.json"))
    asked = serve(monkeypatch, {})
    ccusage = watch_ccusage(monkeypatch, SESSION_BLOCK)
    u = up.AnthropicUsage().fetch(tmp_path, now=lambda: 1000)
    assert u.source == "ccusage"
    assert asked == [] and ccusage == [1]
    assert not marker_file(tmp_path).exists()


def test_a_failed_marker_write_still_falls_through(tmp_path, monkeypatch):
    adapter = two_stores(tmp_path, monkeypatch)
    asked = serve(monkeypatch, {"tok-home": rate_limited(), "tok-host": oauth_response()})

    def unwritable(path, data):
        raise OSError("read-only state dir")

    monkeypatch.setattr(up, "write_json_atomic", unwritable)
    u = adapter.fetch(tmp_path, now=lambda: 1000)
    assert u.source == "oauth"
    assert asked == ["tok-home", "tok-host"]


def test_a_429_keeps_live_entries_and_prunes_dead_ones(tmp_path, monkeypatch):
    adapter = two_stores(tmp_path, monkeypatch, home="tok-a", host="tok-b")
    put_marker(tmp_path, {token_key("tok-b"): 1200, token_key("rotated"): 900,
                          token_key("forged"): 5000})
    serve(monkeypatch, {"tok-a": rate_limited(), "tok-b": oauth_response()})
    adapter.fetch(tmp_path, now=lambda: 1000)
    assert marker(tmp_path) == {token_key("tok-a"): 1300, token_key("tok-b"): 1200}


@pytest.mark.parametrize("retry_after, until, probes", [
    (None, 1300, [(1060, 1), (1300, 2)]),
    (120, 1120, [(1119, 1), (1121, 2)]),
    (999999, 4600, [(4599, 1), (4601, 2)]),
    (0, 1300, [(1299, 1), (1301, 2)]),
])
def test_the_cooldown_length_gates_the_next_requests(
        tmp_path, monkeypatch, retry_after, until, probes):
    adapters = {"anthropic": one_token(tmp_path, monkeypatch, "tok-a")}
    asked = serve(monkeypatch, {"tok-a": rate_limited(retry_after)})
    watch_ccusage(monkeypatch)
    first = up.fetch_provider("anthropic", tmp_path, now=lambda: 1000, adapters=adapters)
    assert first.source == "unavailable" and len(asked) == 1
    assert marker(tmp_path) == {token_key("tok-a"): until}
    for at, requests_so_far in probes:
        u = up.fetch_provider("anthropic", tmp_path, now=lambda: at, adapters=adapters)
        assert u.source == "unavailable"
        assert len(asked) == requests_so_far


@pytest.mark.parametrize("status", [403, 500, None])
def test_other_failures_write_no_marker_and_retry_at_once(tmp_path, monkeypatch, status):
    adapters = {"anthropic": one_token(tmp_path, monkeypatch, "tok-a")}
    asked = serve(monkeypatch, {"tok-a": up.UsageFetchError("x", status=status)})
    ccusage = watch_ccusage(monkeypatch)
    for at in (1000, 1005):
        u = up.fetch_provider("anthropic", tmp_path, now=lambda: at, adapters=adapters)
        assert u.source == "unavailable"
    assert len(asked) == 2 and len(ccusage) == 2
    assert not marker_file(tmp_path).exists()


@pytest.mark.parametrize("content", [
    "not json", "{}", "[]", '{"until": 1100}',
    {token_key("tok-a"): "soon"}, {token_key("tok-a"): None}, {token_key("tok-a"): True}],
    ids=["not-json", "empty-object", "list", "older-shape", "soon", "null", "true"])
def test_a_malformed_marker_does_not_block(tmp_path, monkeypatch, content):
    adapter = one_token(tmp_path, monkeypatch, "tok-a")
    put_marker(tmp_path, content)
    asked = serve(monkeypatch, {"tok-a": oauth_response()})
    u = adapter.fetch(tmp_path, now=lambda: 1000)
    assert u.source == "oauth" and asked == ["tok-a"]


def test_a_marker_path_that_is_a_directory_does_not_block(tmp_path, monkeypatch):
    adapter = one_token(tmp_path, monkeypatch, "tok-a")
    marker_file(tmp_path).mkdir(parents=True)
    asked = serve(monkeypatch, {"tok-a": oauth_response()})
    u = adapter.fetch(tmp_path, now=lambda: 1000)
    assert u.source == "oauth" and asked == ["tok-a"]


@pytest.mark.parametrize("until, blocks", [
    (1000, False),          # expired: at < until is false
    (1000 + 3600, True),    # the cap itself still blocks
    (1000 + 3601, False),   # past the cap: a clock step or a forged entry
])
def test_the_gate_boundaries(tmp_path, monkeypatch, until, blocks):
    adapter = one_token(tmp_path, monkeypatch, "tok-a")
    put_marker(tmp_path, {token_key("tok-a"): until})
    asked = serve(monkeypatch, {"tok-a": oauth_response()})
    watch_ccusage(monkeypatch)
    u = adapter.fetch(tmp_path, now=lambda: 1000)
    assert asked == ([] if blocks else ["tok-a"])
    assert u.source == ("unavailable" if blocks else "oauth")


def test_a_fresh_cached_reading_is_served_during_a_cooldown(tmp_path, monkeypatch):
    adapters = {"anthropic": one_token(tmp_path, monkeypatch, "tok-a")}
    asked = serve(monkeypatch, {"tok-a": oauth_response()})
    assert up.fetch_provider("anthropic", tmp_path, now=lambda: 1000,
                             adapters=adapters).source == "oauth"
    put_marker(tmp_path, {token_key("tok-a"): 1300})
    u = up.fetch_provider("anthropic", tmp_path, now=lambda: 1100, adapters=adapters)
    assert u.source == "oauth"
    assert asked == ["tok-a"]


def test_a_successful_reading_leaves_the_marker_in_place(tmp_path, monkeypatch):
    adapter = two_stores(tmp_path, monkeypatch)
    put_marker(tmp_path, {token_key("tok-home"): 1300})
    serve(monkeypatch, {"tok-host": oauth_response()})
    assert adapter.fetch(tmp_path, now=lambda: 1000).source == "oauth"
    assert marker(tmp_path) == {token_key("tok-home"): 1300}
