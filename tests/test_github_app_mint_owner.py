"""mint refuses a repository its installation's owner doesn't own: the token
request names repositories without owner, so another owner's same-named
repository must never reach it."""
import pytest

from dispatcher import github_app
from tests.test_github_app import PERMS, env, key, kw, pem, reached  # noqa: F401  (fixtures)


@pytest.mark.parametrize("repos", [["acme/analytics"], ["jesdi/analytics", "acme/analytics"]])
def test_mint_refuses_another_owners_repository_before_any_http_call(env, repos):
    inst = reached(env)
    env[0].calls.clear()
    with pytest.raises(ValueError, match="not owned"):
        github_app.mint(inst, repos, PERMS, **kw(env))
    assert env[0].calls == []


def test_mint_matches_the_owner_case_insensitively(env):
    token = github_app.mint(reached(env), ["JESDI/analytics"], PERMS, **kw(env))
    assert token.token == "ghs_analytics"
