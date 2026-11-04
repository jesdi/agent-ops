"""Acceptance tests for ticket 03 (spec S8, R8): dispatcher host work uses
installation tokens, with the stored auth as the fallback for repositories the
installation doesn't reach.

Black-box through `main.run_pass(cfg, deps)`. The real GitHubClient and the real
workspace git run; only the process boundary is faked: `subprocess.run` is
recorded (argv, cwd, the GH_TOKEN the child would see) and `gh` is answered by a
stateful fake of GitHub. Git runs for real against bare "origin" repositories in
tmp_path. Sessions and the notifier are the fakes of test_main.

Interface defined here: `main.Deps` takes `app=`, the GitHub App as the
dispatcher sees it, duck-typed exactly like the one onboarding takes
(`onboard.run(app=)`, see tests/test_onboard_app.py):

    app.registered() -> bool                      # False: no App, stored auth everywhere
    app.installation(repo) -> Installation | None # None: the installation doesn't reach repo
    app.install_link(owner) -> str
    app.mint(installation, repos, permissions) -> Token(token, expires_at)
    any of them may raise github_app.AppError (GitHub answered 500)

`app=None` (the default) means the stored auth, as today.
"""
import json
import logging
import subprocess
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import github, github_app
from dispatcher.state import NO_SLOT, Stage, TaskState, load_all, save

from tests.configfakes import with_board
from tests.fake_github import FakeGitHub as FakeBoardHost
from tests.test_main import FakeNotifier, FakeSessions, cfg, patch_usage

PROJECT_TOKEN = "ghp_project_board"
INSTALL = github_app.Installation(9001, "https://github.com/settings/installations/9001", "jesdi")
REAL_RUN = subprocess.run


TASK_PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write",
                    "workflows": "write", "actions": "read", "checks": "read",
                    "metadata": "read"}


class FakeApp:
    """The installation reaches `reached`; every mint is recorded."""

    def __init__(self, reached=("jesdi/analytics",)):
        self.is_registered = True
        self.reached = set(reached)
        self.fail = {}          # method name -> AppError raised for a repo naming "analytics"
        self.mints = []         # (repos, permissions, token)

    def registered(self):
        return self.is_registered

    def _maybe_fail(self, method, repos):
        if method in self.fail and any("analytics" in r for r in repos):
            raise self.fail[method]

    def installation(self, repo):
        self._maybe_fail("installation", [repo])
        return INSTALL if repo in self.reached else None

    def install_link(self, owner):
        return INSTALL.settings_url

    def mint(self, installation, repos, permissions):
        self._maybe_fail("mint", repos)
        token = f"ghs_host_{len(self.mints) + 1}"
        self.mints.append((list(repos), dict(permissions), token))
        return github_app.Token(token, datetime(2099, 1, 1, tzinfo=timezone.utc))

    def tokens_for(self, repo):
        """The host's mints for `repo`; a task container's own token (ticket 04: the
        A3 permissions, mounted into the container) is not host work."""
        return [t for repos, perms, t in self.mints
                if repo in repos and perms != TASK_PERMISSIONS]


class Call:
    def __init__(self, argv, cwd, env):
        self.argv, self.cwd = list(argv), str(cwd or "")
        self.token = (env or {}).get("GH_TOKEN")      # None: the stored auth
        self.line = " ".join(self.argv)

    def names(self, repo):
        """True when the call is about `repo` (owner/name): named in argv, run in
        its clone, or a graphql read of it."""
        name = repo.split("/")[1]
        return (repo in self.line or Path(self.cwd).name == name
                or f"name={name}" in self.argv)

    @property
    def board(self):
        return self.token == PROJECT_TOKEN


class World:
    """Two projects, one repository each: analytics (the installation reaches
    it) and portfolio_eval (it doesn't). A Ready issue on each board, a PR-open
    task on each, and a recorder of every child process."""

    def __init__(self, tmp_path, monkeypatch, ready=("portfolio_eval", "analytics")):
        self.calls = []
        self.app = FakeApp()
        base = cfg(tmp_path)
        analytics = with_board(dc_replace(
            base.repositories[0], name="analytics", repo="jesdi/analytics", github_id=2,
            clone_path=str(tmp_path / "analytics"), setup_cmd="",
            worktrees_path=str(tmp_path / "analytics.worktrees"),
            project=dc_replace(base.repositories[0].project, name="analytics")), number=2)
        portfolio = dc_replace(base.repositories[0], setup_cmd="")
        self.cfg = dc_replace(base, repositories=[portfolio, analytics])
        self.host = FakeBoardHost()
        for repo, number in (("jesdi/portfolio_eval", 1), ("jesdi/analytics", 2)):
            board = self.host.board("jesdi", number)
            if repo.split("/")[1] in ready:      # a pass claims one Ready issue
                self.host.card(board, self.host.issue(repo, 7, "Ready work"), "Ready")
        for repository in self.cfg.repositories:
            self.make_clone(repository, tmp_path)
        monkeypatch.setenv("GH_PROJECT_TOKEN", PROJECT_TOKEN)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setattr(subprocess, "run", self._run)
        patch_usage(monkeypatch)

    def make_clone(self, repository, tmp_path):
        origin = tmp_path / f"{repository.name}-origin.git"
        for argv, cwd in ((["git", "init", "--bare", "-b", "main", str(origin)], tmp_path),
                          (["git", "clone", str(origin), repository.clone_path], tmp_path)):
            REAL_RUN(argv, cwd=cwd, check=True, capture_output=True)
        clone = repository.clone_path
        for argv in (["git", "commit", "--allow-empty", "-m", "base"],
                     ["git", "push", "origin", "HEAD:main"]):
            REAL_RUN(argv, cwd=clone, check=True, capture_output=True)

    # -- the process boundary ----------------------------------------------------

    def _run(self, argv, *args, **kw):
        call = Call(argv, kw.get("cwd"), kw.get("env"))
        self.calls.append(call)
        if argv[0] == "git":
            return REAL_RUN(argv, *args, **kw)
        try:
            out = self.gh(argv)
        except AssertionError:
            out = "[]"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def gh(self, argv):
        line = " ".join(argv)
        if argv[:3] == ["gh", "api", "user"]:
            return "agent-bot\n"
        if argv[:3] == ["gh", "pr", "view"]:
            return json.dumps({"state": "OPEN", "mergedAt": None, "reviewDecision": "",
                               "reviews": [], "comments": [], "headRefOid": "abc",
                               "headRefName": "agent/task-42"})
        if argv[:3] == ["gh", "project", "item-edit"] or argv[:3] == ["gh", "issue", "comment"]:
            return ""
        if argv[:3] == ["gh", "issue", "view"]:
            return json.dumps({"title": "Ready work", "state": "OPEN"})
        if "actions/runs" in line or "/statuses" in line or argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        out = self.host(argv)
        if "issues(first" in line:    # the fake host keeps no labels: every issue is `auto`
            data = json.loads(out)
            data = data[0] if isinstance(data, list) else data
            for node in data["data"]["repository"]["issues"]["nodes"]:
                node["labels"] = {"nodes": [{"name": "auto"}]}
            return json.dumps([data] if "--slurp" in line else data)
        return out

    # -- a pass --------------------------------------------------------------------

    def pass_(self, app="default"):
        for repository in self.cfg.repositories:   # a PR-open task per project
            self.pr_task(repository)
        self.calls.clear()
        deps = main.Deps(github=github.GitHubClient(), sessions=FakeSessions(),
                         notifier=FakeNotifier())
        deps.app = self.app if app == "default" else app
        main.run_pass(self.cfg, deps)
        return deps

    def pr_task(self, repository):
        if any(t.repository == repository.name and t.issue == 42 for t in load_all(self.cfg.state_dir)):
            return
        wt = Path(repository.worktrees_path) / "task-42"
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        save(self.cfg.state_dir, TaskState(
            issue=42, repository=repository.name, stage=Stage.PR_OPEN, slot=NO_SLOT,
            worktree=str(wt), branch="agent/task-42", title="t", pr_number=12,
            updated_at="2026-07-21T00:00:00+00:00", track="standard"))

    def for_repo(self, repo):
        return [c for c in self.calls if c.names(repo) and not c.board]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def kinds(calls):
    return {("git-fetch" if c.argv[:2] == ["git", "fetch"] else
             "pr-poll" if c.argv[:3] in (["gh", "pr", "view"], ["gh", "pr", "list"])
             or "actions/runs" in c.line or "/statuses" in c.line else
             "label" if c.argv[:2] == ["gh", "label"] else "other") for c in calls}


# --- S8: analytics reached, portfolio_eval not ----------------------------------------

def test_a_reached_repository_s_host_calls_carry_its_installation_token(world):
    world.pass_()
    [token] = world.app.tokens_for("jesdi/analytics")
    analytics = world.for_repo("jesdi/analytics")
    assert {"pr-poll", "label"} <= kinds(analytics)
    assert [c.line for c in analytics if c.token != token] == []


def test_an_unreached_repository_s_host_calls_carry_no_gh_token(world):
    world.pass_()
    portfolio = world.for_repo("jesdi/portfolio_eval")
    assert {"pr-poll", "label"} <= kinds(portfolio)
    assert [c.line for c in portfolio if c.token is not None] == []
    assert [repos for repos, perms, _ in world.app.mints
            if perms != TASK_PERMISSIONS] == [["jesdi/analytics"]]


@pytest.mark.parametrize("ready, clone, reached", [("analytics", "analytics", True),
                                                     ("portfolio_eval", "repo", False)])
def test_the_workspace_git_fetch_carries_the_installation_token_or_none(
        tmp_path, monkeypatch, ready, clone, reached):
    """A claim provisions a workspace: its `git fetch` is host work too."""
    world = World(tmp_path, monkeypatch, ready=(ready,))
    world.pass_()
    fetches = [c for c in world.calls if c.argv[:2] == ["git", "fetch"]]
    assert fetches and {Path(c.cwd).name for c in fetches} == {clone}
    [minted] = world.app.tokens_for("jesdi/analytics")     # the App is in use either way
    expected = minted if reached else None
    assert [c.token for c in fetches] == [expected] * len(fetches)


# --- S3: no App registered -------------------------------------------------------------

def test_with_no_app_registered_the_pass_runs_on_the_stored_auth(world):
    world.app.is_registered = False
    world.pass_()
    portfolio = world.for_repo("jesdi/portfolio_eval")
    assert {"pr-poll", "label"} <= kinds(portfolio)
    assert [c.line for c in world.calls if c.token not in (None, PROJECT_TOKEN)] == []
    assert world.app.mints == []
    assert {"pr-poll", "label"} <= kinds(world.for_repo("jesdi/analytics"))


# --- the App errors --------------------------------------------------------------------

@pytest.mark.parametrize("failing", ["installation", "mint"])
def test_an_app_error_waits_for_the_next_pass_instead_of_falling_back(world, caplog, failing):
    world.app.fail[failing] = github_app.AppError("GET /repos/jesdi/analytics/installation answered 500")
    with caplog.at_level(logging.WARNING):
        world.pass_()
    assert [c.line for c in world.for_repo("jesdi/analytics") if c.token is None] == []
    assert any("analytics" in r.getMessage() for r in caplog.records)
    portfolio = world.for_repo("jesdi/portfolio_eval")        # the other project goes on
    assert {"pr-poll", "label"} <= kinds(portfolio)
    assert all(c.token is None for c in portfolio)


# --- one mint per installation and project -----------------------------------------------

def test_a_pass_mints_one_host_token_for_a_project_however_many_calls(world):
    world.pass_()
    analytics_calls = [c for c in world.for_repo("jesdi/analytics") if c.token]
    assert len(analytics_calls) > 1
    assert len(world.app.tokens_for("jesdi/analytics")) == 1
    assert {c.token for c in analytics_calls} == set(world.app.tokens_for("jesdi/analytics"))


# --- board calls keep the project token ---------------------------------------------------

def test_board_calls_keep_the_project_token(world):
    world.pass_()
    boards = [c for c in world.calls if "projectV2" in c.line or c.argv[:2] == ["gh", "project"]
              or any("projectV2" in a for a in c.argv)]
    assert boards and [c.line for c in boards if c.token != PROJECT_TOKEN] == []
    assert world.app.tokens_for("jesdi/analytics")      # the installation token is minted too
    assert not any(c.token == world.app.tokens_for("jesdi/analytics")[0] for c in boards)
