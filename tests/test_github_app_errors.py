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
    gh = env[0]
    gh.slug = None
    real = gh.__call__

    def http(method, url, headers, body=None):
        if url.endswith("/app"):
            return 404, {"message": "Not Found"}
        return real(method, url, headers, body)
    with pytest.raises(github_app.AppError):
        github_app.install_link("acme", **{**kw(env), "http": http})


@pytest.mark.parametrize("payload", [{}, {"id": 1}, {"account": None, "id": 1}, []])
def test_a_malformed_installation_body_is_an_app_error(env, payload):
    def http(method, url, headers, body=None):
        return 200, payload
    with pytest.raises(github_app.AppError):
        github_app.installation("jesdi/analytics", **{**kw(env), "http": http})


@pytest.mark.parametrize("payload", [{}, {"token": "t"}, {"token": "t", "expires_at": "soon"}])
def test_a_malformed_token_body_is_an_app_error(env, payload):
    inst = reached(env)

    def http(method, url, headers, body=None):
        return 201, payload
    with pytest.raises(github_app.AppError):
        github_app.mint(inst, ["jesdi/analytics"], PERMS, **{**kw(env), "http": http})


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


@pytest.mark.parametrize("repos", [["jesdi/"], ["jesdi/analytics", "jesdi/"]])
def test_mint_refuses_an_empty_repository_name_before_any_http_call(env, repos):
    inst = reached(env)
    env[0].calls.clear()
    with pytest.raises(ValueError):
        github_app.mint(inst, repos, PERMS, **kw(env))
    assert env[0].calls == []


def test_an_unexpected_app_error_never_stops_the_onboarding_run(tmp_path, box):
    app = FakeApp()
    add(tmp_path)
    run(box, sh_for(), app)
    assert step(record(tmp_path), "resolve")["state"] == "verified"
    app.fail = KeyError("slug")
    run(box, sh_for(), app)   # must not raise
    assert step(record(tmp_path), "resolve")["state"] == "verified"
