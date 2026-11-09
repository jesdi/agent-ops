"""No container the dispatcher builds mounts the host's ~/.config/gh, which holds
the box's all-repos PAT. Every launch carries a token directory minted by the
GitHub App, and nothing can be wired without an App.

A dispatcher pass spawns a task and then resumes it through the production
`Sessions` (only herdr's tab is faked, so the typed command is the real
`session_cmd`). A triage sweep builds its argv through the real `triage_cmd`.
Every child process the pass starts is recorded too (the provisioning container
among them).
"""
from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import herdr, onboard
from dispatcher.sessions import Sessions as RealSessions
from dispatcher.state import NO_SLOT, PARK_HUMAN, PARK_WAKE, WHY_OPERATOR, SessionRecord, save, write_session
from tests.test_main import FakeNotifier, FakeSessions
from tests.test_task_container_tokens_acceptance import home  # noqa: F401  (fixture)
from tests.test_task_token_refresh_acceptance import Box
from tests.test_triage_token_acceptance import sweep

GH_MOUNT = "/root/.config/gh"


def gh_sources(line: str) -> list[str]:
    """The host directory of each mount at the container's gh config."""
    return [part.split(":")[0] for part in line.split() if f":{GH_MOUNT}" in part]


def test_a_dispatcher_cannot_be_wired_without_a_github_app():
    with pytest.raises(TypeError, match="app"):
        main.Deps(github=None, sessions=FakeSessions(), notifier=FakeNotifier())


def test_onboarding_cannot_run_without_a_github_app(tmp_path):
    with pytest.raises(TypeError, match="app"):
        onboard.run(tmp_path / "targets.yaml")


def test_no_container_a_dispatcher_pass_launches_mounts_the_host_gh_config(
        tmp_path, monkeypatch, home):  # noqa: F811
    typed = []

    class Tab:
        def run(self, cmd):
            typed.append(cmd)
            return True

    monkeypatch.setattr(herdr.Tab, "ensure", lambda *a, **k: Tab())
    box = Box(tmp_path, monkeypatch, ready=("analytics",))
    real = RealSessions(state_dir=box.cfg.state_dir, clones=box.sessions.clones)
    spawn, resume = box.sessions.spawn_stage, box.sessions.resume

    def real_spawn(*args, **kw):
        spawn(*args, **kw)
        real.spawn_stage(*args, **kw)

    def real_resume(*args, **kw):
        resume(*args, **kw)
        real.resume(*args, **kw)

    box.sessions.spawn_stage, box.sessions.resume = real_spawn, real_resume
    box.spawn_all(("analytics",))
    calls = [c.line for c in box.world.calls]
    write_session(box.cfg.state_dir, "analytics", 7, SessionRecord("recorded-session", "spec"))
    save(box.cfg.state_dir, replace(box.task("analytics"), park=PARK_HUMAN,
                                    park_reason=WHY_OPERATOR, slot=NO_SLOT))
    box.sessions.alive_set.clear()
    box.run()
    save(box.cfg.state_dir, replace(box.task("analytics"), park=PARK_WAKE))
    box.run()
    calls += [c.line for c in box.world.calls]

    assert len(typed) == 2                                   # the spawn, then the resume
    tokens = Path(box.cfg.state_dir) / "tokens"
    for line in typed + calls:
        assert f"{home}/.config/gh" not in line
        assert all(Path(s).parent == tokens for s in gh_sources(line))
    assert all(gh_sources(line) for line in typed)


def test_no_triage_container_mounts_the_host_gh_config(tmp_path, home):  # noqa: F811
    cfg, _, boundary = sweep(tmp_path)
    [argv] = boundary.argvs
    line = " ".join(argv)
    assert f"{home}/.config/gh" not in line
    [source] = gh_sources(line)
    assert Path(source).parent == Path(cfg.state_dir) / "triage-tokens"
