"""The real github_app.Client behind onboarding (spec S3, R4): registered()
needs both the App id and a usable private key, and onboarding blocks at
resolve with the README checklist when either is unreadable."""
import pytest

from dispatcher import github_app, onboard
from tests.test_github_app import NOW, FakeGitHub, FakeSecrets, key, pem  # noqa: F401  (fixtures)
from tests.test_onboard import add, box, record, state, step  # noqa: F401  (box is a fixture)
from tests.test_onboard_app import run, sh_for


class Op(FakeSecrets):
    """FakeSecrets whose `unreadable` fields are absent from the cache, as before the 1Password item exists."""

    def __init__(self, pem, *unreadable):
        super().__init__(pem)
        self.unreadable = set(unreadable)

    def __call__(self, field):
        return "" if field in self.unreadable else super().__call__(field)


def client(op, gh=None):
    return github_app.Client(http=gh or FakeGitHub(), now=lambda: NOW, secret=op)


def test_registered_with_a_readable_id_and_key(pem):
    assert client(Op(pem)).registered()


@pytest.mark.parametrize("field", ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"])
def test_not_registered_when_a_field_is_unreadable(pem, field):
    assert not client(Op(pem, field)).registered()


def test_not_registered_when_the_key_does_not_parse(pem):
    op = Op(pem)
    op.fields["GITHUB_APP_PRIVATE_KEY"] = "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----"
    assert not client(op).registered()


def test_client_calls_github_with_its_seams(pem):
    gh = FakeGitHub()
    app = client(Op(pem), gh)
    inst = app.installation("jesdi/analytics")
    assert inst.id == 9001 and app.installation("jesdi/billing") is None
    assert app.mint(inst, ["jesdi/analytics"], {"contents": "read"}).token == "ghs_analytics"
    assert app.install_link("jesdi") == "https://github.com/settings/installations/9001"


@pytest.mark.parametrize("field", ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"])
def test_onboarding_blocks_at_resolve_when_a_field_is_unreadable(tmp_path, box, pem, field):
    add(tmp_path)
    sh = sh_for()
    run(box, sh, client(Op(pem, field)))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "agent-ops/agent-ops-github-app" in s["detail"]
    assert s["link"] == github_app.README
    assert sh.calls == []


def test_onboarding_with_the_real_client_resolves_and_clones(tmp_path, box, pem):
    add(tmp_path)
    sh = sh_for()
    run(box, sh, client(Op(pem)))
    rec = record(tmp_path)
    assert [step(rec, n)["state"] for n in ("resolve", "clone")] == ["verified", "verified"]
    [(_, _, env)] = sh.ran("git clone")
    assert env["GH_TOKEN"] == "ghs_analytics"


def test_the_unit_builds_the_real_client(tmp_path, box, pem, monkeypatch):
    """onboard.main, the unit's entry, reads the App through github_app's own seams."""
    monkeypatch.setenv("GITHUB_APP_ID", "123456")             # the key's item field is absent:
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "")          # the cache holds it empty
    monkeypatch.setattr(github_app, "_http", lambda *a: pytest.fail("no GitHub call without a key"))
    add(tmp_path)
    onboard.main(["--config", str(box)])
    s = step(record(tmp_path), "resolve")
    assert (s["state"], s["link"]) == ("blocked", github_app.README)
