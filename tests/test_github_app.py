"""Acceptance tests for dispatcher.github_app (ticket 01, spec S1, S2, S4, S5, S14).

Interface under test (all collaborators injected as keyword arguments):

    http(method, url, headers, body) -> (status, payload dict)   # raises TimeoutError on timeout
    now() -> aware datetime
    secret(field) -> str                                         # the unit's cached field; "" = absent

    installation(repo, *, http, now, secret) -> Installation | None  # None = not reached
    mint(installation, repos, permissions, *, http, now, secret) -> Token(token, expires_at: datetime)
    install_link(owner, *, http, now, secret) -> str
    main(["check", "owner/repo"], *, http, now, secret) -> exit code
    AppError: raised on 401, 5xx, timeout; Unregistered (an AppError) on an absent field.
"""
import base64
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from dispatcher import github_app

NOW = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
APP_ID = "123456"
PERMS = {"contents": "read", "issues": "write"}


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


def b64d(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeSecrets:
    """The App fields as a unit loads them from the secret cache; records every field read."""

    def __init__(self, pem, readable=True):
        self.fields = {"GITHUB_APP_ID": APP_ID, "GITHUB_APP_CLIENT_ID": "Iv1.abc",
                       "GITHUB_APP_PRIVATE_KEY": pem}
        self.readable = readable
        self.read = []

    def __call__(self, field):
        self.read.append(field)
        return self.fields[field] if self.readable else ""


class FakeGitHub:
    """installation 9001 on user `jesdi` reaches analytics and analytics-etl, not billing;
    org `acme` has no installation."""

    def __init__(self):
        self.calls = []
        self.fail = None          # status int, or "timeout", for installation lookups
        self.slug = "agent-ops-box"
        self.repo_status = None   # forces the repository read to answer this status

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        path = url.removeprefix("https://api.github.com")
        if self.fail == "timeout":
            raise TimeoutError("timed out")
        if self.fail:
            return self.fail, {"message": "boom"}
        if path in ("/repos/jesdi/analytics/installation", "/repos/jesdi/analytics-etl/installation"):
            return 200, {"id": 9001, "account": {"login": "jesdi", "type": "User"}}
        if path == "/users/jesdi/installation":   # a user account: only the user route answers
            return 200, {"id": 9001, "account": {"login": "jesdi", "type": "User"}}
        if path == "/orgs/acme/installation" and getattr(self, "acme", False):
            return 200, {"id": 77, "account": {"login": "acme", "type": "Organization"}}
        if path == "/app":
            return 200, {"slug": self.slug}
        if path == "/app/installations/9001/access_tokens":
            return 201, {"token": "ghs_analytics", "expires_at": "2026-11-04T11:00:00Z"}
        if path == "/repos/jesdi/analytics" and self.repo_status:
            return self.repo_status, {"message": "denied"}
        if path == "/repos/jesdi/analytics" and headers["Authorization"].split()[-1] == "ghs_analytics":
            return 200, {"full_name": "jesdi/analytics"}
        return 404, {"message": "Not Found"}

    def paths(self):
        return [u.removeprefix("https://api.github.com") for _, u, _, _ in self.calls]

    def jwt(self, method="POST"):
        for m, _, headers, _ in self.calls:
            if m != method:
                continue
            auth = headers.get("Authorization", "")
            if auth.count(".") == 2:
                return auth.split()[-1]
        raise AssertionError("no JWT was sent")


@pytest.fixture
def env(pem):
    return FakeGitHub(), FakeSecrets(pem), (lambda: NOW)


def kw(env):
    gh, op, now = env
    return dict(http=gh, now=now, secret=op)


def reached(env):
    return github_app.installation("jesdi/analytics", **kw(env))


def test_jwt_verifies_with_the_app_claims(env, key):
    inst = reached(env)
    github_app.mint(inst, ["jesdi/analytics"], PERMS, **kw(env))
    head, payload, sig = env[0].jwt().split(".")
    key.public_key().verify(b64d(sig), f"{head}.{payload}".encode(),
                            padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(b64d(head))["alg"] == "RS256"
    claims = json.loads(b64d(payload))
    assert str(claims["iss"]) == APP_ID
    assert claims["iat"] == int(datetime(2026, 11, 4, 9, 59, tzinfo=timezone.utc).timestamp())
    assert claims["exp"] <= int(datetime(2026, 11, 4, 10, 9, tzinfo=timezone.utc).timestamp())
    assert claims["exp"] > claims["iat"]


def test_no_public_call_prints_or_logs_the_key(env, pem, capsys, caplog):
    caplog.set_level("DEBUG")
    body = next(l for l in pem.splitlines() if len(l) > 40)
    inst = reached(env)
    github_app.mint(inst, ["jesdi/analytics"], PERMS, **kw(env))
    github_app.install_link("jesdi", **kw(env))
    github_app.main(["check", "jesdi/analytics"], **kw(env))
    github_app.main(["check", "jesdi/billing"], **kw(env))
    seen = "".join(capsys.readouterr()) + caplog.text
    assert body not in seen and "PRIVATE KEY" not in seen


def test_mint_leaves_no_file_holding_the_key(env, pem, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    github_app.mint(reached(env), ["jesdi/analytics"], PERMS, **kw(env))
    body = next(l for l in pem.splitlines() if len(l) > 40)
    held = [p for d in (state, tmp) for p in d.rglob("*")
            if p.is_file() and body.encode() in p.read_bytes()]
    assert held == []


def test_mint_names_only_its_repository_and_permissions(env):
    token = github_app.mint(reached(env), ["jesdi/analytics"], PERMS, **kw(env))
    posts = [c for c in env[0].calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1] == "https://api.github.com/app/installations/9001/access_tokens"
    assert posts[0][3] == {"repositories": ["analytics"],
                           "permissions": {"contents": "read", "issues": "write"}}
    assert token.token == "ghs_analytics"
    assert token.expires_at == datetime(2026, 11, 4, 11, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("repos", [[], ()])
def test_mint_with_no_repository_is_refused_before_any_http_call(env, repos):
    inst = reached(env)
    env[0].calls.clear()
    with pytest.raises(ValueError):
        github_app.mint(inst, repos, PERMS, **kw(env))
    assert env[0].calls == []


def test_installation_is_not_reached_on_404(env):
    assert github_app.installation("jesdi/billing", **kw(env)) is None


@pytest.mark.parametrize("fail", [401, 500, 502, 503, "timeout"])
def test_installation_raises_on_an_unclear_answer(env, fail):
    env[0].fail = fail
    with pytest.raises(github_app.AppError):
        github_app.installation("jesdi/analytics", **kw(env))


def test_installation_raises_when_the_app_is_not_cached(pem):
    gh = FakeGitHub()
    with pytest.raises(github_app.Unregistered):
        github_app.installation("jesdi/analytics", http=gh, now=lambda: NOW,
                                secret=FakeSecrets(pem, readable=False))


def test_install_link_for_a_user_with_an_installation(env):
    assert github_app.install_link("jesdi", **kw(env)) == \
        "https://github.com/settings/installations/9001"


def test_install_link_for_an_org_with_an_installation(env):
    env[0].acme = True
    assert github_app.install_link("acme", **kw(env)) == \
        "https://github.com/organizations/acme/settings/installations/77"


def test_install_link_without_installation_is_the_app_install_page(env):
    assert github_app.install_link("acme", **kw(env)) == \
        "https://github.com/apps/agent-ops-box/installations/new"


def test_check_passes_every_step_for_a_reached_repository(env, capsys):
    code = github_app.main(["check", "jesdi/analytics"], **kw(env))
    out = capsys.readouterr().out.lower()
    assert code == 0
    for step in ("jwt", "installation", "token", "repository"):
        assert any(step in l and "passed" in l for l in out.splitlines()), step


def test_check_reads_the_repository_with_the_minted_token(env, capsys):
    github_app.main(["check", "jesdi/analytics"], **kw(env))
    reads = [h for m, u, h, _ in env[0].calls
             if m == "GET" and u == "https://api.github.com/repos/jesdi/analytics"]
    assert [h["Authorization"].split()[-1] for h in reads] == ["ghs_analytics"]


def test_check_fails_when_the_repository_read_fails(env, capsys):
    env[0].repo_status = 403
    code = github_app.main(["check", "jesdi/analytics"], **kw(env))
    out = capsys.readouterr().out.lower()
    assert code != 0
    assert not any("repository" in l and "passed" in l for l in out.splitlines())


def test_check_stops_at_the_installation_step_with_the_link(env, capsys):
    code = github_app.main(["check", "jesdi/billing"], **kw(env))
    out = capsys.readouterr().out
    assert code != 0
    assert "https://github.com/settings/installations/9001" in out
    assert "token" not in out.lower()
    assert not any(c[0] == "POST" for c in env[0].calls)
