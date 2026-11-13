"""Ticket 04 beyond the acceptance file: every launch asks for its token before it
changes anything. An App error waits for the next pass and an unreached
repository parks the task for access, at a claim, a stage hand-over, a feedback
round, a retry and a resume. 1Password failing is not the App being unregistered."""
import json
from types import SimpleNamespace

import pytest

import dispatcher.main as main
from dispatcher import github_app, task_tokens
from dispatcher.github import Candidate
from dispatcher.models import Entry
from dispatcher.state import NO_SLOT, PARK_HUMAN, WHY_ACCESS, Stage, TaskState, load, load_all
from tests.test_host_installation_tokens_acceptance import INSTALL, FakeApp
from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, cfg, make_task, patch_usage,
                             patch_workspace, valid_spec)
from tests.test_task_container_tokens_acceptance import (SpawnFailsApp, home, spawn_pass,  # noqa: F401
                                                         world_of)

REPO = SimpleNamespace(name="analytics", repo="jesdi/analytics")
PORTFOLIO = "jesdi/portfolio_eval"


class App(FakeApp):
    """Reaches portfolio_eval; `down` makes every mint answer 500."""

    def __init__(self, reached=(PORTFOLIO,)):
        super().__init__(reached)
        self.down = False

    def mint(self, installation, repos, permissions):
        if self.down:
            raise github_app.AppError("POST access_tokens answered 500")
        return super().mint(installation, repos, permissions)


def deps_of(app, sess=None, gh=None):
    return main.Deps(github=gh or FakeGitHub(), sessions=sess or FakeSessions(alive={42}),
                     notifier=FakeNotifier(), app=app)


def tokens_dir(c, issue=42):
    return str(task_tokens.task_dir(c.state_dir, f"portfolio_eval-{issue}"))


# --- a stage hand-over ------------------------------------------------------------------

def spec_done(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard")
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": "spec.md", "track": "standard"}))
    return c


def test_an_app_error_at_a_stage_hand_over_waits_and_a_later_pass_hands_over(tmp_path, monkeypatch):
    c = spec_done(tmp_path, monkeypatch)
    app = App()
    app.down = True
    sess = FakeSessions(alive={42})
    for _ in range(2):                                   # an outage of two passes
        main.run_pass(c, deps_of(app, sess))
        task = load(c.state_dir, "portfolio_eval", 42)
        assert (task.stage, task.park) == (Stage.SPEC, "")
        assert sess.spawned == [] and sess.ended == []
    app.down = False
    main.run_pass(c, deps_of(app, sess))
    assert [s[:2] for s in sess.spawned] == [(42, "plan")]
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PLAN


def test_an_unreached_repository_at_a_stage_hand_over_parks_the_task_with_the_link(
        tmp_path, monkeypatch):
    c = spec_done(tmp_path, monkeypatch)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps_of(App(reached=()), sess))
    task = load(c.state_dir, "portfolio_eval", 42)
    assert (task.park, task.park_reason) == (PARK_HUMAN, WHY_ACCESS)
    assert INSTALL.settings_url in task.park_note and task.stage is not Stage.FAILED
    assert sess.spawned == []


# --- a feedback round ---------------------------------------------------------------------

def feedback_pending(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=12,
              feedback_pending=True, attention="feedback")
    return c


def test_an_app_error_at_a_feedback_spawn_keeps_the_feedback_and_the_pass_goes_on(
        tmp_path, monkeypatch):
    c = feedback_pending(tmp_path, monkeypatch)
    app = App()
    app.down = True
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps_of(app, sess))                 # does not raise
    task = load(c.state_dir, "portfolio_eval", 42)
    assert (task.stage, task.feedback_pending, task.park) == (Stage.PR_OPEN, True, "")
    assert sess.spawned == [] and sess.ended == []
    app.down = False
    main.run_pass(c, deps_of(app, sess))
    assert [s[:2] for s in sess.spawned] == [(42, "address-review")]
    assert sess.gh_configs[-1] == tokens_dir(c)


def test_an_unreached_repository_at_a_feedback_spawn_parks_it_with_the_feedback_kept(
        tmp_path, monkeypatch):
    c = feedback_pending(tmp_path, monkeypatch)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps_of(App(reached=()), sess))
    task = load(c.state_dir, "portfolio_eval", 42)
    assert (task.park, task.park_reason, task.feedback_pending) == (PARK_HUMAN, WHY_ACCESS, True)
    assert sess.spawned == []


# --- a claim, through an outage of several passes -----------------------------------------

def test_a_claim_waits_out_an_app_outage_of_several_passes_then_launches(
        tmp_path, monkeypatch, home):  # noqa: F811
    world = world_of(tmp_path, monkeypatch, ("analytics",))
    world.app = SpawnFailsApp(world.cfg.repositories[1].worktrees_path)
    world.app.fail["mint"] = github_app.AppError("answered 500")
    for _ in range(3):
        sessions = spawn_pass(world)
        assert sessions.launches == []
        [task] = [t for t in load_all(world.cfg.state_dir) if t.repository == "analytics"]
        assert (task.stage, task.park, task.unlaunched) == (Stage.QUEUED, "", True)
    world.app.fail.clear()
    sessions = spawn_pass(world)
    assert [(x["repository"], x["issue"]) for x in sessions.launches] == [("analytics", 7)]
    [task] = [t for t in load_all(world.cfg.state_dir) if t.repository == "analytics"]
    assert (task.stage, task.unlaunched) == (Stage.SPEC, False)


def test_a_claimed_task_left_queued_without_the_mark_still_fails_as_a_crash(tmp_path, monkeypatch):
    """Only the App's mark launches a queued task: a claim that died for another
    reason keeps today's crash path."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED, session=False)
    sess = FakeSessions()
    main.run_pass(c, deps_of(App(), sess))
    assert sess.spawned == []
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.FAILED


# --- no App registered ----------------------------------------------------------------------

@pytest.mark.parametrize("registered", ["no", "1Password down"])
def test_without_a_usable_app_no_issue_is_claimed(tmp_path, monkeypatch, capsys, registered):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    app = App()
    if registered == "no":
        app.is_registered = False
    else:
        app.registered = lambda: (_ for _ in ()).throw(github_app.AppError("op read timed out"))
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    main.run_pass(c, deps_of(app, FakeSessions(), gh))
    assert gh.claimed == [] and load_all(c.state_dir) == []
    assert "no claims this pass" in capsys.readouterr().err


# --- the App's key comes from the unit's environment (the secret cache) ---------------------

@pytest.mark.parametrize("cached", [None, "", "  \n"], ids=["unset", "empty", "blank"])
def test_an_app_missing_from_the_secret_cache_reads_as_unregistered(monkeypatch, cached):
    """Until the 1Password item exists the cache holds the fields empty; an
    outage keeps the last good copy, so github_app never sees one."""
    for field in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"):
        if cached is None:
            monkeypatch.delenv(field, raising=False)
        else:
            monkeypatch.setenv(field, cached)
    assert github_app.Client().registered() is False


# --- the direct seams ----------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.resumed = []

    def resume(self, *args, **kw):
        self.resumed.append(kw)


def test_resume_passes_the_task_token_dir(tmp_path, monkeypatch):
    cfg_ = SimpleNamespace(state_dir=str(tmp_path))
    deps = SimpleNamespace(access=github_app.Access(FakeApp()), sessions=Recorder())
    task = TaskState(issue=7, repository="analytics", stage=Stage.IMPLEMENT, slot=0,
                     worktree=str(tmp_path), branch="b", title="t", updated_at="")
    monkeypatch.setattr(main, "read_session", lambda *a: SimpleNamespace(stage="implement", session_id="s1"))
    monkeypatch.setattr(main, "_heal_identity", lambda t: None)
    monkeypatch.setattr(main, "_drain", lambda *a: ("", []))
    monkeypatch.setattr(main.messages, "mark_delivered", lambda *a: None)
    monkeypatch.setattr(main.eventlog, "append_event", lambda *a, **k: None)
    launch = main.Launch(Stage.IMPLEMENT, Entry("anthropic", "claude-opus-5"))
    gh_config = main._task_token(cfg_, deps, REPO, task)
    main._continue_or_restart(cfg_, deps, REPO, task, launch, gh_config=gh_config)
    [kw] = deps.sessions.resumed
    assert kw["gh_config"] == str(task_tokens.task_dir(tmp_path, "analytics-7"))
    assert [m[0] for m in deps.access.app.mints] == [["jesdi/analytics"]]


def test_an_unregistered_app_fails_closed_for_a_task(tmp_path):
    app = FakeApp()
    app.is_registered = False
    with pytest.raises(github_app.NoAccess):
        task_tokens.issue(github_app.Access(app), tmp_path, "analytics-7", "jesdi/analytics")
    assert not (tmp_path / "tokens").exists()


@pytest.mark.parametrize("failure", ["app error", "no access"])
def test_a_rejected_signal_retry_touches_nothing_without_a_token(tmp_path, monkeypatch, failure):
    """An App error leaves the rejected signal for the next pass; an unreached
    repository parks the task for access. Neither ends the session or resumes it."""
    app = FakeApp(reached=() if failure == "no access" else ("jesdi/analytics",))
    app.fail["mint"] = github_app.AppError("503")
    task = TaskState(issue=7, repository="analytics", stage=Stage.PLAN, slot=0,
                     worktree="w", branch="b", title="t", updated_at="")
    touched = []
    for name in ("_write_working", "_end_session", "_continued", "save", "_fail_task_crash",
                 "_park_for_input"):
        monkeypatch.setattr(main, name, lambda *a, _n=name, **k: touched.append(_n))
    from dispatcher.lifecycle import Event, EventType
    from dispatcher.loops import Retry
    from dispatcher.state import StageSignal
    from tests.test_lifecycle import admitted, report
    deps = SimpleNamespace(access=github_app.Access(app))
    turn = main._Turn(SimpleNamespace(state_dir=str(tmp_path)), deps, REPO, None, None, False,
                      launches={Stage.PLAN: SimpleNamespace(entry=None)})
    bounced = Event(EventType.SESSION_SIGNAL, report(
        StageSignal("plan", "done"), retries=frozenset({Retry.PLAN_SIGNAL}),
        launches=admitted(Stage.PLAN)))
    main._apply(turn, task, bounced)
    assert touched == ([] if failure == "app error" else ["_park_for_input"])
