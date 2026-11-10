"""Acceptance tests for ticket 04 (spec S9, S9b; R9): a spawned task session sees
GitHub only through a token narrowed to its own repository, mounted in place of
the host's gh config; a task the installation doesn't reach is parked.

Black-box through `main.run_pass(cfg, deps)` on the World of
test_host_installation_tokens_acceptance (real GitHubClient and workspace git, a
recorded process boundary, a FakeApp), plus `containers.session_cmd`.

Interface defined here:

* `containers.session_cmd(..., gh_config=<host dir>)`: the directory is mounted
  read-only at /root/.config/gh and the host's ~/.config/gh is never mounted.
* `Sessions.spawn_stage(..., gh_config=<host dir>)` (and `podman_cmd`): the
  directory main writes `hosts.yml` into, `state_dir/tokens/<task_key>`. main
  passes it whenever the App is registered; the fake here builds its argv with
  the real `session_cmd`.
* a task the installation doesn't reach: `park == PARK_HUMAN`, `park_reason ==
  "access"` (state.WHY_ACCESS) and `park_note` holding the installation link.
* `app.bot_login() -> str`: the App's bot login (`<slug>[bot]`).
"""
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import containers, github
from dispatcher.sessions import session_name
from dispatcher.state import PARK_HUMAN, Stage, load_all, task_key

from tests.test_host_installation_tokens_acceptance import (INSTALL, PROJECT_TOKEN,
                                                            World, FakeApp)
from tests.test_main import FakeNotifier, FakeSessions

A3 = {"contents": "write", "pull_requests": "write", "issues": "write", "workflows": "write",
      "actions": "read", "checks": "read", "metadata": "read"}
STORED_PAT = "ghp_stored_host_pat"
INFRA_TOKEN = "ghp_infra_secret"
OP_TOKEN = "ops_service_account_secret"
APP_KEY = "-----BEGIN RSA PRIVATE KEY-----app-key-material"


class TokenSessions(FakeSessions):
    """Records, for each spawn, the token directory main gave, the hosts.yml it
    held at that moment and the argv the real session_cmd builds from it."""

    def __init__(self, clones):
        super().__init__()
        self.clones, self.launches = clones, []

    def spawn_stage(self, repository, issue, worktree, prompt, stage_name, model,
                    effort="", second=None, *, gh_config):
        super().spawn_stage(repository, issue, worktree, prompt, stage_name, model, effort, second,
                            gh_config=gh_config)
        hosts = Path(gh_config, "hosts.yml")
        argv = containers.session_cmd(session_name(repository, issue), worktree, "2g", "2",
                                      model, "P", clone=self.clones[repository],
                                      gh_config=gh_config)
        self.launches.append(dict(
            repository=repository, issue=issue, gh_config=gh_config, argv=argv,
            hosts=hosts.read_text() if hosts.exists() else None,
            mode=stat.S_IMODE(hosts.stat().st_mode) if hosts.exists() else None))


class BotApp(FakeApp):
    def bot_login(self):
        return "agent-ops[bot]"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A box: the host's gh config holds the stored PAT; the state dir holds the
    1Password token; the host env holds the other secrets."""
    home = tmp_path / "home"
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text(f"github.com:\n  oauth_token: {STORED_PAT}\n")
    (home / ".gitconfig").write_text("[user]\n\tname = agent\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GH_INFRA_TOKEN", INFRA_TOKEN)
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "op-token.env").write_text(f"OP_SERVICE_ACCOUNT_TOKEN={OP_TOKEN}\n")
    (state / "github-app.pem").write_text(APP_KEY)
    return home


def world_of(tmp_path, monkeypatch, ready, app=FakeApp):
    world = World(tmp_path, monkeypatch, ready=ready)
    world.app = app()
    return world


def spawn_pass(world):
    sessions = TokenSessions({r.name: r.clone_path for r in world.cfg.repositories})
    deps = main.Deps(github=github.GitHubClient(), sessions=sessions, notifier=FakeNotifier(),
                     app=world.app)
    world.calls.clear()
    main.run_pass(world.cfg, deps)
    return sessions


def analytics_launch(world, tmp_path):
    sessions = spawn_pass(world)
    [launch] = [x for x in sessions.launches if x["repository"] == "analytics"]
    return launch


def mounts(argv):
    return [m for m in re.findall(r"-v (\S+)", argv)]


# --- S9: an analytics task spawns -----------------------------------------------------

def test_the_spawn_argv_mounts_the_task_token_dir_and_never_the_host_gh_config(
        tmp_path, monkeypatch, home):
    world = world_of(tmp_path, monkeypatch, ("analytics",))
    launch = analytics_launch(world, tmp_path)
    tokens = Path(world.cfg.state_dir) / "tokens" / task_key("analytics", 7)
    assert launch["gh_config"] == str(tokens)
    assert f"-v {tokens}:/root/.config/gh:ro" in launch["argv"]
    assert f"{home}/.config/gh" not in launch["argv"]
    assert [m for m in mounts(launch["argv"]) if m.endswith(":/root/.config/gh:ro")] == [
        f"{tokens}:/root/.config/gh:ro"]


def test_hosts_yml_holds_a_token_minted_for_the_task_repository_with_the_a3_permissions(
        tmp_path, monkeypatch, home):
    world = world_of(tmp_path, monkeypatch, ("analytics",))
    launch = analytics_launch(world, tmp_path)
    assert launch["hosts"] is not None
    [token] = set(re.findall(r"ghs_host_\d+", launch["hosts"]))
    [(repos, permissions, _)] = [m for m in world.app.mints if m[2] == token]
    assert repos == ["jesdi/analytics"]
    assert permissions == A3
    for secret in (STORED_PAT, PROJECT_TOKEN, INFRA_TOKEN, OP_TOKEN):
        assert secret not in launch["hosts"]


def test_the_token_dir_carries_the_session_signing_key_the_gitconfig_names(
        tmp_path, monkeypatch, home):
    """~/.gitconfig signs with /root/.config/gh/agent-ops-signing-key, a path
    inside this mount: without the copy every session's first commit fails."""
    monkeypatch.setattr(Path, "home", lambda: home)   # over conftest's isolated home
    key = home / ".config" / "gh" / "agent-ops-signing-key"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----signing")
    world = world_of(tmp_path, monkeypatch, ("analytics",))
    launch = analytics_launch(world, tmp_path)
    copy = Path(launch["gh_config"], "agent-ops-signing-key")
    assert copy.read_bytes() == key.read_bytes()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600


def test_the_token_dir_carries_the_allowed_signers_the_image_trusts(
        tmp_path, monkeypatch, home):
    """The image sets gpg.ssh.allowedSignersFile=/root/.config/gh/allowed-signers:
    without the copy a session's commits read %G? = N and implement-spec refuses
    to push them."""
    monkeypatch.setattr(Path, "home", lambda: home)   # over conftest's isolated home
    trust = home / ".config" / "gh" / "allowed-signers"
    trust.write_text("agent@example.com ssh-ed25519 AAAAkey\n")
    world = world_of(tmp_path, monkeypatch, ("analytics",))
    launch = analytics_launch(world, tmp_path)
    assert Path(launch["gh_config"], "allowed-signers").read_bytes() == trust.read_bytes()


def test_hosts_yml_is_not_readable_by_other_users(tmp_path, monkeypatch, home):
    world = world_of(tmp_path, monkeypatch, ("analytics",))
    launch = analytics_launch(world, tmp_path)
    assert launch["mode"] is not None and launch["mode"] & 0o077 == 0


def test_no_mount_in_the_spawn_argv_holds_a_secret(tmp_path, monkeypatch, home):
    world = world_of(tmp_path, monkeypatch, ("analytics",))
    launch = analytics_launch(world, tmp_path)
    secrets = (APP_KEY, OP_TOKEN, PROJECT_TOKEN, INFRA_TOKEN, STORED_PAT)
    leaked = []
    for mount in mounts(launch["argv"]):
        source = Path(mount.split(":")[0])
        files = [source] if source.is_file() else [p for p in source.rglob("*") if p.is_file()]
        for path in files:
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            leaked += [f"{path}: {s}" for s in secrets if s in text]
    assert leaked == []
    assert launch["hosts"] is not None      # the dir was mounted: the scan covered it


def test_session_cmd_mounts_the_given_gh_config_dir_read_only_in_place_of_the_host_s(
        tmp_path, monkeypatch, home):
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt = tmp_path / "repos" / "a.worktrees" / "task-7"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: x\n")
    tokens = tmp_path / "state" / "tokens" / "analytics-7"
    tokens.mkdir(parents=True)
    cmd = containers.session_cmd("analytics-7", str(wt), "2g", "2", "claude-fable-5", "P",
                                 gh_config=str(tokens))
    assert f"-v {tokens}:/root/.config/gh:ro" in cmd
    assert f"{home}/.config/gh" not in cmd


# --- S9b: a repository the installation doesn't reach -----------------------------------

def test_a_task_the_installation_does_not_reach_is_parked_with_the_link_and_spawns_once_reached(
        tmp_path, monkeypatch, home):
    world = world_of(tmp_path, monkeypatch, ("portfolio_eval",))     # not reached
    sessions = spawn_pass(world)
    assert sessions.launches == [] and sessions.spawned == []
    [task] = [t for t in load_all(world.cfg.state_dir) if t.repository == "portfolio_eval"]
    assert (task.park, task.park_reason) == (PARK_HUMAN, "access")
    assert INSTALL.settings_url in task.park_note
    tokens = Path(world.cfg.state_dir) / "tokens"
    assert [p for p in tokens.rglob("hosts.yml")] == [] if tokens.exists() else True
    assert not any(STORED_PAT in p.read_text() for p in tokens.rglob("*") if p.is_file())
    # once billing is reached again, a later pass spawns it
    world.app.reached.add("jesdi/portfolio_eval")
    sessions = spawn_pass(world)
    assert [(x["repository"], x["issue"]) for x in sessions.launches] == [("portfolio_eval", 7)]
    [minted] = [m for m in world.app.mints if m[0] == ["jesdi/portfolio_eval"] and m[1] == A3]
    assert minted[2] in sessions.launches[0]["hosts"]


# --- the App errors at spawn ---------------------------------------------------------------

class SpawnFailsApp(FakeApp):
    """Healthy while the claim provisions its workspace; the method in `fail`
    answers 500 once the analytics worktree exists, i.e. at spawn."""

    def __init__(self, worktrees):
        super().__init__()
        self.worktrees = Path(worktrees)

    def _maybe_fail(self, method, repos):
        if self.worktrees.exists() and any(self.worktrees.iterdir()):
            super()._maybe_fail(method, repos)


@pytest.mark.parametrize("failing", ["installation", "mint"])
def test_an_app_error_at_spawn_neither_spawns_nor_parks_and_the_next_pass_retries(
        tmp_path, monkeypatch, home, failing):
    from dispatcher import github_app
    world = world_of(tmp_path, monkeypatch, ("analytics",))
    world.app = SpawnFailsApp(world.cfg.repositories[1].worktrees_path)
    world.app.fail[failing] = github_app.AppError("answered 500")
    sessions = spawn_pass(world)
    assert sessions.launches == [] and sessions.spawned == []
    [task] = [t for t in load_all(world.cfg.state_dir) if t.repository == "analytics"]
    assert (task.park, task.park_reason) == ("", "")
    world.app.fail.clear()
    sessions = spawn_pass(world)
    assert [(x["repository"], x["issue"]) for x in sessions.launches] == [("analytics", 7)]


# --- PR polling ignores the box's own logins ---------------------------------------------------

class PollWorld(World):
    def __init__(self, *a, **kw):
        self.comments = []
        super().__init__(*a, **kw)

    def gh(self, argv):
        if argv[:3] == ["gh", "pr", "view"]:
            payload = json.loads(super().gh(argv))
            payload["comments"] = [{"createdAt": "2026-07-22T00:00:00Z", "author": {"login": login}}
                                   for login in self.comments]
            return json.dumps(payload)
        return super().gh(argv)

    def _run(self, argv, *args, **kw):
        if argv[:3] == ["gh", "api", "user"] and (kw.get("env") or {}).get("GH_TOKEN"):
            raise subprocess.CalledProcessError(403, argv, "", "Resource not accessible")
        return super()._run(argv, *args, **kw)


def poll(tmp_path, monkeypatch, home, comments):
    world = PollWorld(tmp_path, monkeypatch, ready=())
    world.app = BotApp()
    world.comments = comments
    world.pr_task(world.cfg.repositories[1])            # analytics
    sessions = TokenSessions({r.name: r.clone_path for r in world.cfg.repositories})
    deps = main.Deps(github=github.GitHubClient(), sessions=sessions, notifier=FakeNotifier(),
                     app=world.app)
    world.calls.clear()
    main.run_pass(world.cfg, deps)
    return world, sessions


def test_comments_by_the_app_bot_and_the_stored_login_do_not_wake_the_task(
        tmp_path, monkeypatch, home):
    world, sessions = poll(tmp_path, monkeypatch, home, ["agent-ops[bot]", "agent-bot"])
    assert sessions.spawned == [] and sessions.resumed == []
    [task] = [t for t in load_all(world.cfg.state_dir) if t.repository == "analytics"]
    assert task.stage is Stage.PR_OPEN


def test_a_comment_by_any_other_login_still_counts_as_feedback(tmp_path, monkeypatch, home):
    world, sessions = poll(tmp_path, monkeypatch, home, ["agent-ops[bot]", "reviewer-jane"])
    assert sessions.spawned or sessions.resumed


def test_the_box_s_logins_are_resolved_without_gh_api_user_on_an_installation_token(
        tmp_path, monkeypatch, home):
    world, _ = poll(tmp_path, monkeypatch, home, ["agent-bot"])
    tokens = {m[2] for m in world.app.mints}
    users = [c for c in world.calls if c.argv[:3] == ["gh", "api", "user"]]
    assert [c.line for c in users if c.token in tokens] == []
