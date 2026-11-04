"""Acceptance tests for ticket 05 (spec S11, S12, S9, S13; R10): a running task's
token is refreshed before it expires, swapped in by rename, and the task's token
directory goes with its session.

Black-box through `main.run_pass(cfg, deps)` on the World of
test_host_installation_tokens_acceptance (real GitHubClient and workspace git, a
recorded process boundary), with the sessions of test_task_container_tokens_acceptance
(their argv is built by the real `containers.session_cmd`) and a FakeApp whose
mints expire `lifetime` minutes after the injected clock.

Interface defined here:

* `main.Deps(clock=<callable returning an aware datetime>)`: the time the pass
  measures a token's remaining life against. None (the default) is the real now.
* A pass re-mints, for each task holding a session, the token whose `expires_at`
  is under 20 minutes from `deps.clock()` (exactly 20 is not under).
* `state_dir/tokens/<task_key>` exists only while the task holds a session
  (`state.holds_slot`); a pass removes any other directory under `tokens/`.
"""
import os
import re
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import containers, github, github_app
from dispatcher.sessions import session_name
from dispatcher.state import (NO_SLOT, PARK_HUMAN, PARK_WAKE, WHY_OPERATOR, Stage, TaskState,
                              load_all, save, task_key, write_session, SessionRecord)

from tests.test_host_installation_tokens_acceptance import (INSTALL, TASK_PERMISSIONS, FakeApp,
                                                            World)
from tests.test_main import FakeNotifier, FakeSessions
from tests.test_task_container_tokens_acceptance import PollWorld, home  # noqa: F401  (fixture)

T0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
BOTH = ("jesdi/analytics", "jesdi/portfolio_eval")


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class ExpiringApp(FakeApp):
    """Mints expire `lifetime[repo]` minutes after the clock; the installation reaches both."""

    def __init__(self, clock, lifetime):
        super().__init__(reached=BOTH)
        self.clock, self.lifetime = clock, lifetime

    def mint(self, installation, repos, permissions):
        token = super().mint(installation, repos, permissions)
        minutes = self.lifetime.get(repos[0], 60)
        return github_app.Token(token.token, self.clock() + timedelta(minutes=minutes))

    def task_mints(self, repo=None):
        return [(r, t) for r, p, t in self.mints
                if p == TASK_PERMISSIONS and (repo is None or r == [repo])]


class Sessions(FakeSessions):
    """Sessions that stay alive once launched and record every launch's argv (built by
    the real session_cmd) and the hosts.yml it mounts at that moment, spawn or resume."""

    def __init__(self, clones):
        super().__init__()
        self.clones, self.launches = clones, []

    def _launch(self, repository, issue, worktree, model, gh_config):
        extra = {"gh_config": gh_config} if gh_config else {}
        argv = containers.session_cmd(session_name(repository, issue), worktree, "2g", "2",
                                      model, "P", clone=self.clones[repository], **extra)
        hosts = Path(gh_config, "hosts.yml") if gh_config else None
        self.launches.append(dict(
            repository=repository, issue=issue, gh_config=gh_config, argv=argv,
            hosts=hosts.read_text() if hosts and hosts.exists() else None))
        self.alive_set.add(issue)

    def spawn_stage(self, repository, issue, worktree, prompt, stage_name, model, effort="",
                    second=None, gh_config=""):
        super().spawn_stage(repository, issue, worktree, prompt, stage_name, model, effort, second)
        self._launch(repository, issue, worktree, model, gh_config)

    def resume(self, repository, issue, worktree, message, model, effort="", second=None,
               *, session_id=None, gh_config=""):
        super().resume(repository, issue, worktree, message, model, effort, second,
                       session_id=session_id)
        self._launch(repository, issue, worktree, model, gh_config)

    def end(self, repository, issue):
        super().end(repository, issue)
        self.alive_set.discard(issue)


class Box:
    """Two reached projects, one Ready issue each, one persistent Sessions, an injected clock."""

    def __init__(self, tmp_path, monkeypatch, lifetime=None, ready=("analytics", "portfolio_eval"),
                 world=World):
        self.clock = Clock()
        self.world = world(tmp_path, monkeypatch, ready=ready)
        self.app = self.world.app = ExpiringApp(self.clock, lifetime or {})
        self.cfg = self.world.cfg
        self.tokens = Path(self.cfg.state_dir) / "tokens"
        self.sessions = Sessions({r.name: r.clone_path for r in self.cfg.repositories})

    def run(self):
        deps = main.Deps(github=github.GitHubClient(), sessions=self.sessions,
                         notifier=FakeNotifier())
        deps.app, deps.clock = self.app, self.clock
        self.world.calls.clear()
        main.run_pass(self.cfg, deps)

    def spawn_all(self, repos=("analytics", "portfolio_eval")):
        for _ in range(4):
            self.run()
            if {x["repository"] for x in self.sessions.launches} >= set(repos):
                return
        raise AssertionError(f"launched only {[x['repository'] for x in self.sessions.launches]}")

    def dir(self, repo, issue=7):
        return self.tokens / task_key(repo, issue)

    def hosts(self, repo, issue=7):
        return (self.dir(repo, issue) / "hosts.yml").read_text()

    def task(self, repo, issue=7):
        [task] = [t for t in load_all(self.cfg.state_dir) if (t.repository, t.issue) == (repo, issue)]
        return task


@pytest.fixture
def box(tmp_path, monkeypatch, home):  # noqa: F811
    # analytics' token has 19 minutes left after 39; portfolio_eval's 21
    return Box(tmp_path, monkeypatch, lifetime={"jesdi/analytics": 58, "jesdi/portfolio_eval": 60})


# --- S11: refresh at the 20-minute boundary ----------------------------------------------

def test_a_pass_re_mints_only_the_token_with_under_20_minutes_left_and_swaps_it_by_rename(box):
    box.spawn_all()
    old = {r: box.hosts(r) for r in ("analytics", "portfolio_eval")}
    inode = {r: os.stat(box.dir(r) / "hosts.yml").st_ino for r in old}
    reader = open(box.dir("analytics") / "hosts.yml")           # a reader holding the old file
    minted = len(box.app.task_mints())
    box.clock.advance(minutes=39)                                # 19 and 21 minutes left
    box.run()
    with reader:
        assert reader.read() == old["analytics"]                 # complete old token, not partial
    assert box.app.task_mints()[minted:] == [(["jesdi/analytics"], box.app.task_mints()[-1][1])]
    [fresh] = re.findall(r"ghs_host_\d+", box.hosts("analytics"))
    assert fresh == box.app.task_mints()[-1][1] and fresh not in old["analytics"]
    assert os.stat(box.dir("analytics") / "hosts.yml").st_ino != inode["analytics"]
    assert box.hosts("portfolio_eval") == old["portfolio_eval"]
    assert os.stat(box.dir("portfolio_eval") / "hosts.yml").st_ino == inode["portfolio_eval"]
    assert os.listdir(box.dir("analytics")) == ["hosts.yml"]
    assert stat.S_IMODE(os.stat(box.dir("analytics") / "hosts.yml").st_mode) & 0o077 == 0


def test_a_refreshed_token_is_not_minted_again_on_the_next_pass(box):
    box.spawn_all()
    box.clock.advance(minutes=39)
    box.run()
    minted = len(box.app.task_mints())
    box.run()
    assert len(box.app.task_mints()) == minted


def test_exactly_20_minutes_left_is_not_refreshed_one_second_less_is(tmp_path, monkeypatch, home):
    box = Box(tmp_path, monkeypatch, lifetime={"jesdi/analytics": 60}, ready=("analytics",))
    box.spawn_all(("analytics",))
    minted = len(box.app.task_mints())
    box.clock.advance(minutes=40)                                # exactly 20:00 left
    box.run()
    assert len(box.app.task_mints()) == minted
    box.clock.advance(seconds=1)                                 # 19:59 left
    box.run()
    assert len(box.app.task_mints()) == minted + 1


def test_a_refresh_that_fails_with_an_app_error_keeps_the_current_file_and_retries(box):
    box.spawn_all()
    old = box.hosts("analytics")
    box.clock.advance(minutes=39)
    box.app.fail["mint"] = github_app.AppError("answered 500")
    box.run()
    assert box.hosts("analytics") == old
    assert os.listdir(box.dir("analytics")) == ["hosts.yml"]     # no temp file left behind
    assert box.task("analytics").park == ""
    box.app.fail.clear()
    box.run()
    assert box.hosts("analytics") != old
    assert re.findall(r"ghs_host_\d+", box.hosts("analytics")) == [box.app.task_mints()[-1][1]]


# --- S12 and the parks: the directory goes with the session --------------------------------

def test_the_token_dir_goes_with_a_terminal_task_and_no_mint_is_made_for_it(
        tmp_path, monkeypatch, home):
    box = Box(tmp_path, monkeypatch, lifetime={"jesdi/analytics": 58}, ready=("analytics",))
    box.spawn_all(("analytics",))
    assert box.dir("analytics").is_dir()
    save(box.cfg.state_dir, replace(box.task("analytics"), stage=Stage.DONE, slot=NO_SLOT))
    box.sessions.alive_set.clear()
    minted = len(box.app.task_mints())
    box.clock.advance(minutes=39)                                # its token would be due
    box.run()
    assert not box.dir("analytics").exists()
    assert len(box.app.task_mints()) == minted


def test_a_parked_task_loses_its_token_dir_and_gets_a_fresh_token_when_respawned(
        tmp_path, monkeypatch, home):
    box = Box(tmp_path, monkeypatch, ready=("analytics",))
    box.spawn_all(("analytics",))
    old = box.hosts("analytics")
    minted = len(box.app.task_mints())
    save(box.cfg.state_dir, replace(box.task("analytics"), park=PARK_HUMAN, park_reason=WHY_OPERATOR,
                                    slot=NO_SLOT))
    box.sessions.alive_set.clear()
    box.clock.advance(minutes=45)                                # its token would be due
    box.run()
    assert not box.dir("analytics").exists()
    assert len(box.app.task_mints()) == minted                   # none for a parked task
    save(box.cfg.state_dir, replace(box.task("analytics"), park=PARK_WAKE))
    box.run()
    last = box.sessions.launches[-1]
    assert len(box.sessions.launches) == 2 and last["repository"] == "analytics"
    assert last["gh_config"] == str(box.dir("analytics"))
    assert last["hosts"] and last["hosts"] != old
    assert re.findall(r"ghs_host_\d+", last["hosts"]) == [box.app.task_mints()[-1][1]]


def test_a_token_dir_with_no_slot_holding_task_is_removed_by_the_next_pass(box):
    box.spawn_all()
    leftover = box.tokens / task_key("analytics", 99)             # a crash left it behind
    leftover.mkdir()
    (leftover / "hosts.yml").write_text("github.com:\n    oauth_token: ghs_leftover\n")
    box.run()
    assert not leftover.exists()
    assert box.dir("analytics").is_dir() and box.dir("portfolio_eval").is_dir()


# --- S9, S13: a resumed session mounts its own task token ---------------------------------------

def feedback_resume(tmp_path, monkeypatch, home):
    box = Box(tmp_path, monkeypatch, ready=(), world=PollWorld)
    box.world.comments = ["reviewer-jane"]
    task_world = box.world
    task_world.pr_task(box.cfg.repositories[1])                  # analytics, PR open, issue 42
    write_session(box.cfg.state_dir, "analytics", 42, SessionRecord("recorded-session", "address-review"))
    box.run()
    return box


def test_operator_feedback_launches_the_session_with_its_task_token_dir_and_never_the_host_gh(
        tmp_path, monkeypatch, home):
    box = feedback_resume(tmp_path, monkeypatch, home)
    [launch] = box.sessions.launches          # a fresh address-review launch or a resume
    tokens = box.dir("analytics", 42)
    assert launch["gh_config"] == str(tokens)
    assert f"-v {tokens}:/root/.config/gh:ro" in launch["argv"]
    assert f"{home}/.config/gh" not in launch["argv"]
    [minted] = [t for r, t in box.app.task_mints("jesdi/analytics")]
    assert re.findall(r"ghs_host_\d+", launch["hosts"]) == [minted]


def test_a_woken_session_resumes_with_a_token_minted_at_resume(tmp_path, monkeypatch, home):
    box = Box(tmp_path, monkeypatch, ready=("analytics",))
    box.spawn_all(("analytics",))
    old = box.hosts("analytics")
    write_session(box.cfg.state_dir, "analytics", 7, SessionRecord("recorded-session", "spec"))
    save(box.cfg.state_dir, replace(box.task("analytics"), park=PARK_HUMAN, park_reason=WHY_OPERATOR,
                                    slot=NO_SLOT))
    box.sessions.alive_set.clear()
    box.run()                                                    # the dir is pruned
    save(box.cfg.state_dir, replace(box.task("analytics"), park=PARK_WAKE))
    box.run()
    assert box.sessions.resumed
    last = box.sessions.launches[-1]
    tokens = box.dir("analytics")
    assert f"-v {tokens}:/root/.config/gh:ro" in last["argv"]
    assert f"{home}/.config/gh" not in last["argv"]
    assert last["hosts"] and last["hosts"] != old


def test_the_real_sessions_resume_mounts_the_task_token_dir_and_never_the_host_gh(
        tmp_path, monkeypatch, home):
    """Only the herdr boundary is faked: the resume runs through the production
    Sessions.resume -> _launch, so a gh_config that is dropped on the way shows up in the argv."""
    from dispatcher import herdr
    from dispatcher.sessions import Sessions as RealSessions

    typed = []

    class FakeTab:
        def run(self, cmd):
            typed.append(cmd)
            return True

    monkeypatch.setattr(herdr.Tab, "ensure", lambda *a, **k: FakeTab())
    box = Box(tmp_path, monkeypatch, ready=("analytics",))
    box.spawn_all(("analytics",))
    real = RealSessions(state_dir=box.cfg.state_dir, clones=box.sessions.clones)
    box.sessions.resume = lambda repository, issue, worktree, message, model, effort="", \
        second=None, *, session_id=None, gh_config="": real.resume(
            repository, issue, worktree, message, model, effort, second,
            session_id=session_id, gh_config=gh_config)
    write_session(box.cfg.state_dir, "analytics", 7, SessionRecord("recorded-session", "spec"))
    save(box.cfg.state_dir, replace(box.task("analytics"), park=PARK_HUMAN, park_reason=WHY_OPERATOR,
                                    slot=NO_SLOT))
    box.sessions.alive_set.clear()
    box.run()                                                    # the dir is pruned
    save(box.cfg.state_dir, replace(box.task("analytics"), park=PARK_WAKE))
    box.run()
    [cmd] = typed
    assert f"-v {box.dir('analytics')}:/root/.config/gh:ro" in cmd
    assert f"{home}/.config/gh" not in cmd
