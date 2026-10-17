"""Ticket 05 fixer: forged repository declarations, per-repository clones,
retention of qualified records, branch ownership and legacy migration."""
import json
from dataclasses import replace

import pytest

from dispatcher import containers, main, state, tmux_migration
from dispatcher.sessions import Sessions
from dispatcher.waitd import handle_ping
from tests.test_execution_identity_acceptance import BACK, BACK_LABEL, FRONT, N, T, Herdr, podman  # noqa: F401
from tests.test_main import ADMIT_ALL, FakeSessions, cfg, deps, patch_usage
from tests.test_main import make_task as make_cfg_task
from tests.webfakes import make_config, make_task


def ping(d, **kw):
    handle_ping(json.dumps({"issue": N, "target": T, **kw}).encode(), d)


def test_a_declaration_without_the_launch_token_writes_nothing(tmp_path):
    for r in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=N, target=T, repo=r))
    token = state.issue_execution_token(tmp_path, T, N, BACK)
    ping(tmp_path, repo=BACK, session_id="from-front", token="guess")
    ping(tmp_path, repo=BACK, session_id="from-front")
    assert state.read_session(tmp_path, T, N, repo=BACK) is None
    assert not state.has_waiting(tmp_path, T, N, repo=BACK)
    ping(tmp_path, repo=BACK, session_id="back-conv", token=token)
    assert state.read_session(tmp_path, T, N, repo=BACK).session_id == "back-conv"
    assert state.has_waiting(tmp_path, T, N, repo=BACK)


def test_a_repository_picks_its_own_clone(tmp_path, monkeypatch, podman):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n")
    (wt / ".agent").mkdir()
    server = Herdr(monkeypatch, [BACK_LABEL])
    front, back = tmp_path / "front", tmp_path / "back"
    front.mkdir()
    back.mkdir()
    s = Sessions(state_dir=tmp_path, clones={T: str(front), BACK: str(back)})
    s.spawn_stage(T, N, str(wt), "p", "implement", "claude-opus-5", repo=BACK)
    run = server.launched()[0][3]
    assert f"{back}:{back}" in run and str(front) not in run
    assert json.loads((wt / ".agent" / "task.json").read_text())["repo"] == BACK
    assert (wt / ".agent" / "execution-token").read_text() == (
        (tmp_path / "execution-tokens" / state.task_key(T, N, BACK)).read_text())


def test_flush_removes_the_qualified_task_and_conversation(tmp_path):
    from tests.test_main import cfg
    c = replace(cfg(tmp_path), done_retention_days=0)
    old = make_task(issue=N, target=T, repo=BACK, stage=state.Stage.DONE,
                    updated_at="2000-01-01T00:00:00+00:00")
    state.save(c.state_dir, old)
    state.write_session(c.state_dir, T, N, state.SessionRecord("c", "implement"), repo=BACK)
    main._flush_finished(c)
    assert state.read_session(c.state_dir, T, N, repo=BACK) is None
    assert state.load_all(c.state_dir) == []


def test_branch_ownership_comes_from_the_launched_issue(tmp_path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "task.json").write_text(json.dumps({"branch": "agent/task-12"}))
    assert containers.task_branch(str(tmp_path), "task-x-99", 12) == "agent/task-12"
    assert containers.task_branch(str(tmp_path), "task-x-12", 99) == ""


def test_migration_leaves_a_legacy_session_owned_by_the_sibling(tmp_path, monkeypatch):
    for r in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=N, target=T, repo=r))
    state.record_session_owner(tmp_path, T, N, FRONT)
    woken = []
    monkeypatch.setattr(tmux_migration, "_has_session", lambda n: n == f"task-{T}-{N}")
    monkeypatch.setattr(tmux_migration, "_kill_session", lambda n: f"killed {n}")
    monkeypatch.setattr(tmux_migration, "_remove_container", lambda n: f"removed {n}")
    tmux_migration.migrate(tmp_path, lambda task, msg: woken.append(task.repo))
    assert woken == [FRONT]


def test_a_legacy_ping_ignores_sibling_tokens_but_rejects_any_token(tmp_path):
    for r in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=N, target=T, repo=r))
    state.record_session_owner(tmp_path, T, N, FRONT)
    token = state.issue_execution_token(tmp_path, T, N, BACK)
    ping(tmp_path, session_id="forged", token=token)
    assert state.read_session(tmp_path, T, N, repo=FRONT) is None
    ping(tmp_path, session_id="legacy-conv")
    assert state.read_session(tmp_path, T, N, repo=FRONT).session_id == "legacy-conv"


def test_a_legacy_ping_from_a_foreign_sender_writes_nothing(tmp_path):
    for r in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=N, target=T, repo=r))
    state.record_session_owner(tmp_path, T, N, FRONT)
    for extra in ({"session_id": "forged"}, {"background_tasks": [{"id": "b1"}]}):
        body = json.dumps({"issue": N, "target": T, **extra}).encode()
        handle_ping(body, tmp_path, legacy_sender=lambda target, issue: False)
        for repo in (FRONT, BACK, ""):
            assert state.read_session(tmp_path, T, N, repo=repo) is None
            assert not state.has_waiting(tmp_path, T, N, repo=repo)
            assert state.read_background(tmp_path, T, N, repo=repo) is None
    handle_ping(json.dumps({"issue": N, "target": T, "session_id": "own"}).encode(),
                tmp_path, legacy_sender=lambda target, issue: True)
    assert state.read_session(tmp_path, T, N, repo=FRONT).session_id == "own"
    assert state.has_waiting(tmp_path, T, N, repo=FRONT)


@pytest.mark.parametrize("ends_successfully", [True, False])
@pytest.mark.parametrize("legacy", [True, False])
def test_waking_an_owned_live_login_parked_session_ends_it_before_launch(
        tmp_path, monkeypatch, ends_successfully, legacy):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_cfg_task(c, park=state.PARK_WAKE, repo=c.targets[0].repo)
    task = state.load(c.state_dir, "portfolio_eval", 42, repo=c.targets[0].repo)
    if legacy:
        state.record_session_owner(c.state_dir, task.target, task.issue, task.repo)
    else:
        state.issue_execution_token(c.state_dir, task.target, task.issue, task.repo)
    state.write_session(c.state_dir, task.target, task.issue,
                        state.SessionRecord("recorded", "implement"), repo=task.repo)
    sess = FakeSessions(alive=[task.issue])
    original_end = sess.end

    def end(target, issue, repo=""):
        original_end(target, issue, repo)
        if ends_successfully:
            sess.alive_set.discard(issue)

    monkeypatch.setattr(sess, "end", end)
    main._resume_woken(c, deps(sess=sess), admit=ADMIT_ALL, order=tuple)
    assert sess.ended
    assert bool(sess.resumed) is ends_successfully
