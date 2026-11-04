"""Onboarding checks GitHub App access and works with installation tokens
(ticket 02, spec S3-S7, R4-R7).

Interface under test: `onboard.run(config, sh=, get=, now=, app=)`. `app` is the
GitHub App as onboarding sees it, a duck-typed object (the real one wraps
dispatcher.github_app):

    app.registered() -> bool                      # False: the 1Password item is unreadable
    app.installation(repo) -> Installation | None # None: the installation doesn't reach repo
    app.install_link(owner) -> str                # the owner's installation settings, else the install page
    app.mint(installation, repos, permissions) -> Token(token, expires_at)
    any of them may raise github_app.AppError: GitHub or 1Password gave no clear answer

The fake below is stateful: the operator adds and removes repositories, and
every mint is recorded.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dispatcher import github_app, onboard
from tests.test_onboard import (  # noqa: F401  (box and infra_token are fixtures)
    LIVE, NOW, FakeSh, add, box, cloned, gh_repo, infra_routes, infra_token, intent,
    no_get, record, state, step)

PERMS = {"contents": "read", "issues": "write", "metadata": "read"}
INSTALL_PAGE = "https://github.com/apps/agent-ops-box/installations/new"
JESDI = github_app.Installation(9001, "https://github.com/settings/installations/9001", "jesdi")
ACME = github_app.Installation(77, "https://github.com/organizations/acme/settings/installations/77", "acme")


class FakeApp:
    def __init__(self, reached=("jesdi/analytics",), installations=(JESDI,)):
        self.is_registered = True
        self.installations = {i.owner: i for i in installations}
        self.reached = set(reached)
        self.fail = None          # an AppError to raise from installation()
        self.mint_fail = None     # an AppError to raise from mint()
        self.mints = []           # (installation id, repos, permissions, token)

    def add(self, *repos):
        self.reached.update(repos)

    def remove(self, *repos):
        self.reached.difference_update(repos)

    def registered(self):
        return self.is_registered

    def installation(self, repo):
        if self.fail:
            raise self.fail
        inst = self.installations.get(repo.split("/")[0])
        return inst if inst and repo in self.reached else None

    def install_link(self, owner):
        inst = self.installations.get(owner)
        return inst.settings_url if inst else INSTALL_PAGE

    def mint(self, installation, repos, permissions):
        if self.mint_fail:
            raise self.mint_fail
        assert all(r.split("/")[0] == installation.owner for r in repos), "repo of another owner"
        token = f"ghs_{installation.id}_{len(self.mints) + 1}"
        self.mints.append((installation.id, list(repos), dict(permissions), token))
        return github_app.Token(token, datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc))

    def token_for(self, repo):
        [token] = [t for _, repos, _, t in self.mints if repo in repos]
        return token


def run(box, sh, app, now=NOW):
    onboard.run(box, sh=sh, get=no_get, now=now, app=app)


REPOS = {"jesdi/analytics": 202, "jesdi/analytics-etl": 203, "jesdi/billing": 204, "acme/web": 303}


def origin(argv, cwd, env):
    name = Path(argv[2]).name.removesuffix(".partial")
    return f"https://github.com/{next(r for r in REPOS if r.split('/')[1] == name)}.git\n"


def sh_for():
    sh = FakeSh({"remote get-url origin": origin})
    for r, i in REPOS.items():   # route() goes first: analytics-etl must beat analytics
        sh.route(f"gh api repos/{r}", gh_repo(i, r))
    return sh


# -- S3: no App ---------------------------------------------------------------


def test_without_a_registered_app_resolve_is_blocked_with_the_checklist(tmp_path, box):
    app = FakeApp()
    app.is_registered = False
    add(tmp_path)
    sh = sh_for()
    run(box, sh, app)
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked"
    assert "agent-ops/agent-ops-github-app" in s["detail"]
    assert s["link"].startswith("https://github.com/jesdi/agent-ops-infra") and "README" in s["link"]
    assert sh.ran("git clone") == [] and app.mints == []


# -- S4: a repository outside the installation --------------------------------


def test_a_repository_outside_the_installation_blocks_then_resumes(tmp_path, box):
    app = FakeApp(reached=("jesdi/analytics",))
    add(tmp_path, project="billing", repos=("jesdi/billing",))
    sh = sh_for()
    run(box, sh, app)
    s = step(record(tmp_path, "billing"), "resolve")
    assert (s["state"], s["link"]) == ("blocked", JESDI.settings_url)
    assert "jesdi/billing" in s["detail"]
    assert sh.ran("git clone") == [] and app.mints == []
    app.add("jesdi/billing")
    sh = sh_for()
    run(box, sh, app)
    rec = record(tmp_path, "billing")
    assert [step(rec, n)["state"] for n in ("resolve", "clone")] == ["verified", "verified"]
    assert len(sh.ran("git clone")) == 1


# -- S5: an owner with no installation ----------------------------------------


def test_an_owner_without_an_installation_links_to_the_install_page(tmp_path, box):
    app = FakeApp()
    add(tmp_path, project="web", repos=("acme/web",))
    sh = sh_for()
    run(box, sh, app)
    s = step(record(tmp_path, "web"), "resolve")
    assert (s["state"], s["link"]) == ("blocked", INSTALL_PAGE)
    assert sh.ran("git clone") == []


# -- an App error is not a block ----------------------------------------------


@pytest.mark.parametrize("where", ["fail", "mint_fail"])
def test_an_app_error_fails_the_step_and_is_retried(tmp_path, box, where):
    app = FakeApp()
    setattr(app, where, github_app.AppError("GET /repos/jesdi/analytics/installation answered 500"))
    add(tmp_path)
    sh = sh_for()
    run(box, sh, app)
    s = step(record(tmp_path), "resolve")
    assert (s["state"], s["link"]) == ("failed", "")
    assert sh.ran("git clone") == []
    setattr(app, where, None)
    run(box, sh_for(), app)
    rec = record(tmp_path)
    assert [step(rec, n)["state"] for n in ("resolve", "clone")] == ["verified", "verified"]


# -- S6: removed after confirm ------------------------------------------------


def test_a_repository_removed_after_confirm_blocks_the_board_step(tmp_path, box):
    app = FakeApp(reached=("jesdi/analytics", "jesdi/analytics-etl"))
    add(tmp_path, repos=("jesdi/analytics", "jesdi/analytics-etl"))
    run(box, sh_for(), app)
    rec = record(tmp_path)
    rec["confirmed_revision"] = rec["revision"]      # the operator confirmed
    onboard.write_json_atomic(state(tmp_path) / "onboarding" / "analytics.json", rec)
    app.remove("jesdi/analytics-etl")
    sh = sh_for()
    run(box, sh, app)
    s = step(record(tmp_path), "board")
    assert (s["state"], s["link"]) == ("blocked", JESDI.settings_url)
    assert "jesdi/analytics-etl" in s["detail"]
    board = sh.github.boards[("jesdi", 4)]
    assert sh.github.labels == {} and board["items"] == [] and board["repos"] == set()
    assert sh.ran("gh label create") == []


# -- S7: each call carries its own credential ---------------------------------


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "project-tok")
    monkeypatch.setenv("GH_INFRA_TOKEN", "infra-tok")
    monkeypatch.setenv("GH_REPO_TOKEN", "stored-repo-tok")


def token(env):
    return (env or {}).get("GH_TOKEN")


def kind(argv):
    line = " ".join(argv)
    if "agent-ops-infra" in line or "onboarding/infra" in line:
        return "infra"
    if "api graphql" in line:
        return "issues" if "issues(first" in line else "board"
    if "git clone" in line or "gh label" in line or "gh api repos/" in line:
        return "clone" if "git clone" in line else "repo"
    if argv[0] == "gh" or {"clone", "fetch", "push"} & set(argv):
        return "infra"      # every other network call is the infra PR's
    return "local"


def test_each_call_carries_its_own_credential(tmp_path, box, tokens, infra_token):
    app = FakeApp()
    add(tmp_path)
    first = sh_for()
    run(box, first, app)
    minted_first = [t for *_, t in app.mints]
    rec = record(tmp_path)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    second = sh_for()
    second.routes = {**second.routes, **infra_routes()}
    run(box, second, app)
    minted_second = [t for *_, t in app.mints][len(minted_first):]
    assert len(minted_first) == len(minted_second) == 1
    seen = set()
    for sh, minted in ((first, minted_first[0]), (second, minted_second[0])):
        for argv, _, env in sh.calls:
            k = kind(argv)
            want = {"infra": "infra-tok", "board": "project-tok", "issues": minted,
                    "repo": minted, "clone": minted}.get(k)
            seen.add(k)
            if k != "local":
                assert token(env) == want, (k, argv)
            assert token(env) != "stored-repo-tok", argv
    assert {"infra", "board", "issues", "repo", "clone"} <= seen, seen


# -- narrowed mints -----------------------------------------------------------


def test_the_mint_requests_only_the_projects_repositories_and_permissions(tmp_path, box):
    app = FakeApp(reached=("jesdi/analytics", "jesdi/analytics-etl"))
    add(tmp_path, repos=("jesdi/analytics", "jesdi/analytics-etl"))
    run(box, sh_for(), app)
    [(inst, repos, perms, _)] = app.mints
    assert (inst, sorted(repos), perms) == (9001, ["jesdi/analytics", "jesdi/analytics-etl"], PERMS)


def test_repositories_under_two_owners_get_one_token_per_installation(tmp_path, box, tokens):
    app = FakeApp(reached=("jesdi/analytics", "acme/web"), installations=(JESDI, ACME))
    add(tmp_path, repos=("jesdi/analytics", "acme/web"))
    sh = sh_for()
    run(box, sh, app)
    assert sorted((i, r) for i, r, _, _ in app.mints) == [(77, ["acme/web"]), (9001, ["jesdi/analytics"])]
    assert all(p == PERMS for _, _, p, _ in app.mints)
    assert len(sh.ran("git clone")) == 2
    for argv, _, env in sh.ran("git clone") + sh.ran("gh api repos/"):
        repo = next(r for r in REPOS if r in " ".join(argv))
        assert token(env) == app.token_for(repo), argv
