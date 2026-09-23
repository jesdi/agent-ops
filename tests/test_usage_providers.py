import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dispatcher import usage_providers as up
from dispatcher.config import Config, Target
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
                  session_cpus="2", targets=[],
                  **({"models": models} if models else {}))


def one_track_policy(*entries: str):
    from dispatcher.models import parse_policy
    stage = list(entries)
    return parse_policy({"triage": stage, "untracked": "t", "tracks": {
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


def boom(url, headers):
    raise up.UsageFetchError("dark")


def test_unavailable_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", boom)
    monkeypatch.setattr(up, "_ccusage_json", lambda: None)
    adapters = {"anthropic": up.AnthropicUsage(credentials_path=creds(tmp_path))}
    u = up.fetch_provider("anthropic", tmp_path, adapters=adapters)
    assert u.source == "unavailable"
    assert not (tmp_path / "usage" / "anthropic.json").exists()


def test_falls_back_to_ccusage_with_a_session_window_only(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", boom)
    monkeypatch.setattr(up, "_ccusage_json", lambda: {"blocks": [
        {"isActive": True, "projection": {"remainingMinutes": 45}, "percentUsed": 62.0}]})
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "ccusage"
    (w,) = u.windows
    assert (w.kind, w.length) == (WindowKind.SESSION, timedelta(hours=5))
    assert abs(w.used - 0.62) < 1e-9
    assert 44 <= (w.resets_at - datetime.now(timezone.utc)).total_seconds() / 60 <= 46


def test_both_dark_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", boom)
    monkeypatch.setattr(up, "_ccusage_json", lambda: None)
    assert up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path).source == "unavailable"


def test_missing_credentials_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_ccusage_json", lambda: None)
    assert up.AnthropicUsage(credentials_path=tmp_path / "nope.json").fetch(tmp_path).source == "unavailable"


def test_fetch_all_covers_only_referenced_providers(tmp_path):
    fake = FakeUsage()
    c = cfg(tmp_path, models=one_track_policy("fake/m"))
    out = up.fetch_all(c, adapters={"fake": fake, "anthropic": FakeUsage()})
    assert set(out) == {"fake"} and fake.calls == 1


def test_fetch_all_reports_missing_adapter_as_unavailable(tmp_path):
    c = cfg(tmp_path, models=one_track_policy(
        "nvidia/claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"))
    out = up.fetch_all(c, adapters={"anthropic": FakeUsage()})
    assert out["nvidia"].source == "unavailable"
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


def fake_op(tmp_path, output="op-tok", fail=False):
    """Fake `op` binary logging calls; AGENT_OPS_OP points usage_providers at it."""
    calls = tmp_path / "op-calls.log"
    op = tmp_path / "op"
    body = "exit 1" if fail else f"echo '{output}'"
    op.write_text("#!/bin/sh\n"
                  f"echo \"op $@ token=$OP_SERVICE_ACCOUNT_TOKEN\" >> {calls}\n"
                  f"{body}\n")
    op.chmod(0o755)
    return op, calls


def test_op_token_preferred_over_credentials_store(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    op, calls = fake_op(tmp_path)
    monkeypatch.setenv("AGENT_OPS_OP", str(op))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "svc-tok")
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "oauth"
    assert seen["headers"]["Authorization"] == "Bearer op-tok"
    assert "agent-ops-claude/CLAUDE_CODE_OAUTH_TOKEN" in calls.read_text()


def test_op_token_failure_falls_back_to_store_token(tmp_path, monkeypatch):
    tried = []

    def fake_get(url, headers):
        tried.append(headers["Authorization"])
        if headers["Authorization"] == "Bearer op-tok":
            raise up.UsageFetchError("401")
        return oauth_response()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    op, _ = fake_op(tmp_path)
    monkeypatch.setenv("AGENT_OPS_OP", str(op))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "svc-tok")
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "oauth"
    assert tried == ["Bearer op-tok", "Bearer tok-123"]


def test_op_skipped_without_service_account_token(tmp_path, monkeypatch):
    # web has no op plumbing; usage_providers must not shell out to an op that
    # can only hang or prompt — it falls straight to the store.
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    op, calls = fake_op(tmp_path)
    monkeypatch.setenv("AGENT_OPS_OP", str(op))
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert seen["headers"]["Authorization"] == "Bearer tok-123"
    assert not calls.exists()


def test_op_empty_or_failing_read_is_ignored(tmp_path, monkeypatch):
    seen = {}

    def fake_get(url, headers):
        seen["headers"] = headers
        return oauth_response()

    monkeypatch.setattr(up, "_http_get_json", fake_get)
    op, _ = fake_op(tmp_path, output="")
    monkeypatch.setenv("AGENT_OPS_OP", str(op))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "svc-tok")
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert seen["headers"]["Authorization"] == "Bearer tok-123"


# ---- fail closed on unreadable readings ------------------------------------

@pytest.mark.parametrize("payload", [{"limits": []}, {}])
def test_an_empty_oauth_reading_is_not_oauth(tmp_path, monkeypatch, payload):
    """A 200 with zero windows would admit every model; it fails closed like
    a rejected token: next token, then ccusage, then unavailable."""
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: payload)
    monkeypatch.setattr(up, "_ccusage_json", lambda: None)
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "unavailable" and u.windows == ()


def test_an_empty_oauth_reading_tries_the_next_token(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: (
        {"limits": []} if headers["Authorization"] == "Bearer env-tok" else FIXTURE))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok")
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path)
    assert u.source == "oauth" and len(u.windows) == 3


def test_an_empty_oauth_reading_falls_back_to_ccusage(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: {})
    monkeypatch.setattr(up, "_ccusage_json", lambda: {"blocks": [
        {"isActive": True, "projection": {"remainingMinutes": 45}, "percentUsed": 62.0}]})
    assert up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path).source == "ccusage"


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
def test_a_malformed_anthropic_payload_is_unavailable_not_a_raise(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(up, "_http_get_json", lambda url, headers: payload)
    monkeypatch.setattr(up, "_ccusage_json", lambda: None)
    adapters = {"anthropic": up.AnthropicUsage(credentials_path=creds(tmp_path))}
    assert up.fetch_provider("anthropic", tmp_path, adapters=adapters).source == "unavailable"


def test_ccusage_used_is_clamped_and_resets_follow_the_injected_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_http_get_json", boom)
    monkeypatch.setattr(up, "_ccusage_json", lambda: {"blocks": [
        {"isActive": True, "projection": {"remainingMinutes": 45}, "percentUsed": 130}]})
    u = up.AnthropicUsage(credentials_path=creds(tmp_path)).fetch(tmp_path, now=lambda: 1_000_000.0)
    (w,) = u.windows
    assert w.used == 1.0
    assert w.resets_at == datetime.fromtimestamp(1_000_000.0, timezone.utc) + timedelta(minutes=45)


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
