"""github_app's error boundary: anything GitHub, 1Password or the key can get
wrong comes out as AppError (retried, never "blocked with a link"), and no
message carries the key or op's output. An unexpected error from the App never
stops onboarding of every other project (final review of 8a)."""
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from dispatcher import github_app
from tests.test_github_app import PERMS, FakeOp, env, key, kw, pem, reached  # noqa: F401
from tests.test_onboard import add, box, record, step  # noqa: F401
from tests.test_onboard_app import FakeApp, run, sh_for


def test_a_404_on_get_app_is_an_app_error(env):
    real = env[0].__call__

    def http(method, url, headers, body=None):
        if url.endswith("/app"):
            return 404, {"message": "Not Found"}
        return real(method, url, headers, body)
    with pytest.raises(github_app.AppError):
        github_app.install_link("acme", **{**kw(env), "http": http})


@pytest.mark.parametrize("payload", [{}, {"id": 1}, {"account": None, "id": 1}, [],
                                     {"id": 1, "account": {"login": None, "type": "User"}}])
def test_a_malformed_installation_body_is_an_app_error(env, payload):
    def http(method, url, headers, body=None):
        return 200, payload
    with pytest.raises(github_app.AppError):
        github_app.installation("jesdi/analytics", **{**kw(env), "http": http})


@pytest.mark.parametrize("payload", [
    {}, {"token": "t"}, {"token": "t", "expires_at": "soon"},
    {"token": "t", "expires_at": "2026-11-04T11:00:00"},   # naive: would break the cache
    {"token": None, "expires_at": "2026-11-04T11:00:00Z"},
    {"token": "", "expires_at": "2026-11-04T11:00:00Z"}])
def test_a_malformed_token_body_is_an_app_error(env, payload):
    inst = reached(env)

    def http(method, url, headers, body=None):
        return 201, payload
    with pytest.raises(github_app.AppError):
        github_app.mint(inst, ["jesdi/analytics"], PERMS, **{**kw(env), "http": http})


@pytest.mark.parametrize("payload", [{}, {"slug": None}, {"slug": ""}])
def test_get_app_without_a_slug_is_an_app_error(env, payload):
    real = env[0].__call__

    def http(method, url, headers, body=None):
        return (200, payload) if url.endswith("/app") else real(method, url, headers, body)
    with pytest.raises(github_app.AppError):
        github_app.install_link("acme", **{**kw(env), "http": http})


@pytest.mark.parametrize("status", [404, 403])
def test_check_names_the_failed_repository_read(env, capsys, status):
    env[0].repo_status = status
    assert github_app.main(["check", "jesdi/analytics"], **kw(env)) != 0
    assert "repository read: failed" in capsys.readouterr().out


@pytest.mark.parametrize("repo", ["jesdi/", "/analytics", "jesdi/a/b"])
def test_check_refuses_a_repository_that_is_not_owner_name(env, repo, capsys):
    assert github_app.main(["check", repo], **kw(env)) == 2
    assert env[0].calls == []


def test_a_key_of_an_unsupported_type_is_an_app_error(env):
    other = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    gh, _, now = env
    with pytest.raises(github_app.AppError) as exc:
        github_app.installation("jesdi/analytics", http=gh, now=now, sh=FakeOp(other))
    assert "PRIVATE KEY" not in str(exc.value)
    assert not github_app.Client(http=gh, now=now, sh=FakeOp(other)).registered()


def test_op_stderr_stays_out_of_the_error(env):
    gh, _, now = env

    def op(argv, cwd=None, env=None):
        raise subprocess.CalledProcessError(1, argv, output="secret-out", stderr="secret-err")
    with pytest.raises(github_app.AppError) as exc:
        github_app.installation("jesdi/analytics", http=gh, now=now, sh=op)
    text = str(exc.value) + repr(exc.value.__cause__) + repr(exc.value.__context__)
    assert "secret" not in text


@pytest.mark.parametrize("repos", [["jesdi/"], ["jesdi/analytics", "jesdi/"], ["jesdi"], ["/analytics"]])
def test_mint_refuses_an_empty_repository_name_before_any_http_call(env, repos):
    inst = reached(env)
    env[0].calls.clear()
    with pytest.raises(ValueError):
        github_app.mint(inst, repos, PERMS, **kw(env))
    assert env[0].calls == []


class Flaky(FakeApp):
    """Raises a non-AppError for jesdi/analytics only, once `broken` is set."""
    broken = False

    def installation(self, repo):
        if self.broken and repo == "jesdi/analytics":
            raise KeyError("slug")
        return super().installation(repo)


def test_an_unexpected_app_error_fails_the_step_and_the_other_projects_go_on(tmp_path, box):
    app = Flaky(reached=("jesdi/analytics", "jesdi/analytics-etl"))
    add(tmp_path)
    run(box, sh_for(), app)
    assert step(record(tmp_path), "resolve")["state"] == "verified"
    at = next(s["name"] for s in record(tmp_path)["steps"] if s["state"] != "verified")
    app.broken = True
    add(tmp_path, project="etl", repos=("jesdi/analytics-etl",), epoch_ms=2000)
    run(box, sh_for(), app)   # must not raise
    s = step(record(tmp_path), at)
    assert (s["state"], s["link"]) == ("failed", "")   # retried next run, never blocked with a link
    assert "KeyError" in s["detail"]
    assert step(record(tmp_path, "etl"), "resolve")["state"] == "verified"
    app.broken = False
    run(box, sh_for(), app)
    assert step(record(tmp_path), at)["state"] != "failed"


def test_an_app_error_building_the_install_link_fails_resolve_without_a_link(tmp_path, box):
    app = FakeApp(reached=())

    def broken(owner):
        raise github_app.AppError("GET /app answered 404")
    app.install_link = broken
    add(tmp_path)
    run(box, sh_for(), app)
    s = step(record(tmp_path), "resolve")
    assert (s["state"], s["link"]) == ("failed", "")
