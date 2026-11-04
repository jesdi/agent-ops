"""A resumed task session mounts its own narrowed token, never the host's gh config;
an unreadable App fails closed for a task launch."""
from types import SimpleNamespace

import pytest

import dispatcher.main as main
from dispatcher import github_app
from dispatcher.state import Stage, TaskState
from tests.test_host_installation_tokens_acceptance import FakeApp

REPO = SimpleNamespace(name="analytics", repo="jesdi/analytics")


class Recorder:
    def __init__(self):
        self.resumed = []

    def resume(self, *args, **kw):
        self.resumed.append(kw)


def test_resume_passes_the_task_token_dir(tmp_path, monkeypatch):
    cfg = SimpleNamespace(state_dir=str(tmp_path))
    deps = SimpleNamespace(app=FakeApp(), sessions=Recorder())
    task = TaskState(issue=7, repository="analytics", stage=Stage.IMPLEMENT, slot=0,
                     worktree="w", branch="b", title="t", updated_at="")
    monkeypatch.setattr(main, "read_session", lambda *a: SimpleNamespace(stage="implement", session_id="s1"))
    monkeypatch.setattr(main, "_heal_identity", lambda t: None)
    monkeypatch.setattr(main, "_drain", lambda *a: ("", []))
    monkeypatch.setattr(main.messages, "mark_delivered", lambda *a: None)
    monkeypatch.setattr(main.eventlog, "append_event", lambda *a, **k: None)
    launch = SimpleNamespace(model="m", entry=SimpleNamespace(effort=""), second=None)
    main._continue_or_restart(cfg, deps, REPO, task, launch)
    [kw] = deps.sessions.resumed
    assert kw["gh_config"].endswith("analytics-7") or "tokens" in kw["gh_config"]
    assert [m[0] for m in deps.app.mints] == [["jesdi/analytics"]]


def test_an_unregistered_app_fails_closed_for_a_task(tmp_path):
    app = FakeApp()
    app.is_registered = False
    with pytest.raises(github_app.NoAccess):
        main._task_gh_config(SimpleNamespace(state_dir=str(tmp_path)), SimpleNamespace(app=app), REPO, 7)


def test_a_rejected_signal_retry_waits_out_an_app_error(tmp_path, monkeypatch):
    """The retry touches nothing on an App error: the rejected signal stays, the next pass retries."""
    app = FakeApp()
    app.fail["mint"] = github_app.AppError("503")
    task = TaskState(issue=7, repository="analytics", stage=Stage.PLAN, slot=0,
                     worktree="w", branch="b", title="t", updated_at="")
    touched = []
    for name in ("_write_working", "_end_session", "_continue_or_restart", "save", "_fail_task_crash"):
        monkeypatch.setattr(main, name, lambda *a, _n=name, **k: touched.append(_n))
    act = SimpleNamespace(stage=Stage.PLAN, reason="r", retry=None)
    launch = SimpleNamespace(entry=None)
    main._retry_stage(SimpleNamespace(state_dir=str(tmp_path)), SimpleNamespace(app=app), REPO, task, launch, act)
    assert touched == []
