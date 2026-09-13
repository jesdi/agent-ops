import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import usage_providers as up
from dispatcher.config import Config, Target
from dispatcher.usage import SESSION
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


def cfg(tmp_path, models=None, triage_model=""):
    return Config(state_dir=str(tmp_path), capacity=1, budget_threshold=0.8,
                  racing_minutes=30, racing_threshold=0.95, session_memory="2g",
                  session_cpus="2", targets=[], triage_model=triage_model,
                  **({"models": models} if models else {}))


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
    assert (w.kind, w.length) == ("session", SESSION)
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
    from dispatcher.models import parse_policy
    c = cfg(tmp_path, models=parse_policy({"default": "fake/m"}))
    out = up.fetch_all(c, adapters={"fake": fake, "anthropic": FakeUsage()})
    assert set(out) == {"fake"} and fake.calls == 1


def test_fetch_all_reports_missing_adapter_as_unavailable(tmp_path):
    c = cfg(tmp_path, triage_model="nvidia/claude-sonnet-4-6")
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
