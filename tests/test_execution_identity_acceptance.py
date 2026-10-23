"""Acceptance tests for ticket 05 (project-onboarding): execution-facing identity.

Spec: specs/project-onboarding/spec.md requirements 3 and 24, scenario S05.
Seam: SESSION -- Sessions.spawn_stage/resume/end and waitd.handle_ping, driven
over a faked herdr/podman edge and a tmp state dir.

Interface these tests define (the ticket leaves exact shapes open):
- Every Sessions operation that names a task takes `repo=""` (keyword). A
  non-empty repo selects the execution label `task-<state.task_key(T, N, repo)>`
  (the spelling the snapshot already uses). An empty repo keeps `task-<T>-<N>`.
- A live legacy session (label `task-<T>-<N>`) is addressed by a qualified
  call only when `state.owns_task_session` proves that repo owns it.
- A Stop ping carries the trusted execution's `repo` ("owner/name"). waitd
  writes the session record and the waiting/background markers under
  `state.task_key(target, issue, repo)` and only when that task exists; a
  declaration that matches no task writes nothing, anywhere.
- `main._prune_snapshots` keeps the qualified snapshot of an in-flight task.
"""
import json
import os
import re
import subprocess as sp
import time
from pathlib import Path

import pytest

import dispatcher
import dispatcher.sessions as sessions
from dispatcher import herdr, state
from dispatcher.sessions import Sessions
from dispatcher.waitd import handle_ping
from tests.webfakes import make_config, make_task

T, N = "shop", 12
FRONT, BACK = "example/shop-frontend", "example/shop-backend"
FRONT_LABEL = sessions.session_name(T, N, FRONT)
BACK_LABEL = sessions.session_name(T, N, BACK)
LEGACY_LABEL = "task-shop-12"
# What Podman accepts as a container name (`--name`).
PODMAN_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")


class Herdr:
    """A herdr server holding labelled tabs; records every call."""

    def __init__(self, monkeypatch, labels, workspaces=(T, "agent-ops")):
        self.workspaces = workspaces
        self.tabs = {label: i for i, label in enumerate(labels, start=2)}
        self.calls = []
        monkeypatch.setattr(herdr, "_run", self._run)

    def _ok(self, args, out):
        return sp.CompletedProcess(["herdr", *args], 0, out, "")

    def _run(self, args):
        self.calls.append(args)
        env = lambda result: json.dumps({"id": "i", "result": result})
        head = args[:2]
        if head == ["workspace", "list"]:
            return self._ok(args, env({"type": "workspace_list", "workspaces": [
                {"label": label, "workspace_id": "w1", "number": 1, "focused": True,
                 "pane_count": 1, "tab_count": 1, "active_tab_id": "w1:t2",
                 "agent_status": "unknown"} for label in self.workspaces]}))
        if head == ["tab", "list"]:
            return self._ok(args, env({"type": "tab_list", "tabs": [
                {"label": label, "tab_id": f"w1:t{n}", "workspace_id": "w1", "number": n,
                 "focused": False, "pane_count": 1, "agent_status": "working"}
                for label, n in self.tabs.items()]}))
        if head == ["pane", "list"]:
            return self._ok(args, env({"type": "pane_list", "panes": [
                {"pane_id": f"w1:p{n}", "tab_id": f"w1:t{n}", "workspace_id": "w1",
                 "agent_status": "working", "focused": False, "revision": 1,
                 "terminal_id": str(n)} for n in self.tabs.values()]}))
        if head == ["pane", "process-info"]:
            return self._ok(args, env({"type": "pane_process_info", "process_info": {
                "foreground_process_group_id": 200, "shell_pid": 100, "pane_id": args[2],
                "foreground_processes": [{"name": "x", "pid": 200}]}}))
        if head == ["pane", "read"]:
            return self._ok(args, f"history of {args[2]}\n")
        if head == ["agent", "get"]:
            return self._ok(args, env({"type": "agent_info", "agent": {
                "agent_status": "idle", "state_change_seq": 7}}))
        if head == ["pane", "run"]:
            return self._ok(args, "")
        if head == ["tab", "close"]:
            return self._ok(args, env({"type": "ok"}))
        return None

    def closed(self):
        return [c[2] for c in self.calls if c[:2] == ["tab", "close"]]

    def launched(self):
        return [c for c in self.calls if c[:2] == ["pane", "run"]]


@pytest.fixture
def podman(monkeypatch):
    calls = []
    real = sessions.subprocess.run

    def run(argv, *a, **kw):
        if argv and argv[0] == "podman":
            calls.append(argv)
            return sp.CompletedProcess(argv, 0, b"", b"")
        return real(argv, *a, **kw)

    monkeypatch.setattr(sessions.subprocess, "run", run)
    return calls


def removed(podman):
    return [c[-1] for c in podman if c[:3] == ["podman", "rm", "-f"]]


def test_s05_development_checkout_is_not_the_task_clone(tmp_path, monkeypatch, podman):
    runtime = Path(dispatcher.__file__).resolve().parent.parent
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n")
    server = Herdr(monkeypatch, ["task-agent-ops-5"])
    s = Sessions(state_dir=tmp_path, clones={"agent-ops": str(runtime)})
    with pytest.raises(Exception, match="runtime checkout"):
        s.spawn_stage("agent-ops", 5, str(wt), "prompt", "implement", "claude-opus-5")
    assert server.launched() == []


def test_same_number_issues_get_separate_tabs_and_containers(tmp_path, monkeypatch, podman):
    server = Herdr(monkeypatch, [FRONT_LABEL, BACK_LABEL])
    Sessions(state_dir=tmp_path).end(T, N, repo=BACK)
    assert server.closed() == ["w1:t3"]
    assert removed(podman) == [BACK_LABEL]
    assert (tmp_path / "snapshots" / f"task-{state.task_key(T, N, BACK)}.txt").read_text() \
        == "history of w1:p3"
    assert not (tmp_path / "snapshots" / f"task-{state.task_key(T, N, FRONT)}.txt").exists()


def test_execution_labels_are_distinct_and_valid_container_names():
    assert FRONT_LABEL != BACK_LABEL != LEGACY_LABEL
    for label in (FRONT_LABEL, BACK_LABEL):
        assert PODMAN_NAME.fullmatch(label), f"Podman rejects --name {label}"


def test_qualified_liveness_reads_only_its_own_tab(tmp_path, monkeypatch):
    Herdr(monkeypatch, [FRONT_LABEL])
    s = Sessions(state_dir=tmp_path)
    assert s.is_alive(T, N, repo=FRONT) is True
    assert s.is_alive(T, N, repo=BACK) is False


def test_qualified_resume_launches_under_its_own_label(tmp_path, monkeypatch, podman):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n")
    server = Herdr(monkeypatch, [FRONT_LABEL, BACK_LABEL])
    clone = tmp_path / "clone"
    clone.mkdir()
    s = Sessions(state_dir=tmp_path, clones={T: str(clone)})
    s.resume(T, N, str(wt), "go on", "claude-opus-5", session_id="abc", repo=BACK)
    (run,) = server.launched()
    assert run[2] == "w1:p3"
    assert f"--name {BACK_LABEL}" in run[3] or f"--name={BACK_LABEL}" in run[3]
    assert FRONT_LABEL not in run[3]


def test_fresh_qualified_launch_keeps_the_sibling_conversation(tmp_path, monkeypatch, podman):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n")
    Herdr(monkeypatch, [BACK_LABEL])
    clone = tmp_path / "clone"
    clone.mkdir()
    state.write_session(tmp_path, T, N, state.SessionRecord("front-conv", "implement"), repo=FRONT)
    state.write_session(tmp_path, T, N, state.SessionRecord("back-conv", "implement"), repo=BACK)
    Sessions(state_dir=tmp_path, clones={T: str(clone)}).spawn_stage(
        T, N, str(wt), "p", "implement", "claude-opus-5", repo=BACK)
    assert state.read_session(tmp_path, T, N, repo=BACK) is None
    assert state.read_session(tmp_path, T, N, repo=FRONT).session_id == "front-conv"


def _save_both(d):
    for r in (FRONT, BACK):
        state.save(d, make_task(issue=N, target=T, repo=r))


def _ping(d, **kw):
    handle_ping(json.dumps({"issue": N, "target": T, **kw}).encode(), d)


def test_stop_ping_writes_to_the_trusted_executions_identity(tmp_path):
    _save_both(tmp_path)
    token = state.issue_execution_token(tmp_path, T, N, BACK)
    _ping(tmp_path, repo=BACK, token=token, session_id="back-conv")
    assert state.read_session(tmp_path, T, N, repo=BACK).session_id == "back-conv"
    assert state.has_waiting(tmp_path, T, N, repo=BACK)
    assert state.read_session(tmp_path, T, N, repo=FRONT) is None
    assert not state.has_waiting(tmp_path, T, N, repo=FRONT)
    assert not state.has_waiting(tmp_path, T, N)


def test_background_report_is_qualified_too(tmp_path):
    _save_both(tmp_path)
    token = state.issue_execution_token(tmp_path, T, N, FRONT)
    _ping(tmp_path, repo=FRONT, token=token, background_tasks=[{"id": "b1"}])
    assert state.read_background(tmp_path, T, N, repo=FRONT).tasks == ("b1",)
    assert state.read_background(tmp_path, T, N, repo=BACK) is None
    assert state.read_background(tmp_path, T, N) is None


def test_mismatched_declaration_does_not_redirect_the_write(tmp_path):
    state.save(tmp_path, make_task(issue=N, target=T, repo=FRONT))
    state.save(tmp_path, make_task(issue=N, target=T))  # a legacy, unqualified record
    _ping(tmp_path, repo=BACK, session_id="stray")
    assert state.read_session(tmp_path, T, N, repo=FRONT) is None
    assert state.read_session(tmp_path, T, N, repo=BACK) is None
    assert state.read_session(tmp_path, T, N) is None
    assert not state.has_waiting(tmp_path, T, N, repo=FRONT)
    assert not state.has_waiting(tmp_path, T, N)
    # BACK has no task here: the declaration must leave no trace under it.
    assert not state.has_waiting(tmp_path, T, N, repo=BACK)
    assert state.read_background(tmp_path, T, N, repo=BACK) is None
    _ping(tmp_path, repo=BACK, background_tasks=[{"id": "b1"}])
    for repo in (FRONT, BACK, ""):
        assert state.read_background(tmp_path, T, N, repo=repo) is None
        assert not state.has_waiting(tmp_path, T, N, repo=repo)


def test_sibling_token_or_no_token_cannot_write_to_a_launched_execution(tmp_path):
    _save_both(tmp_path)
    front_token = state.issue_execution_token(tmp_path, T, N, FRONT)
    back_token = state.issue_execution_token(tmp_path, T, N, BACK)
    _ping(tmp_path, repo=BACK, token=front_token, session_id="forged")
    _ping(tmp_path, repo=BACK, session_id="forged")
    _ping(tmp_path, repo=BACK, token="wrong", background_tasks=[{"id": "b1"}])
    for repo in (FRONT, BACK, ""):
        assert state.read_session(tmp_path, T, N, repo=repo) is None
        assert not state.has_waiting(tmp_path, T, N, repo=repo)
        assert state.read_background(tmp_path, T, N, repo=repo) is None
    _ping(tmp_path, repo=BACK, token=back_token, session_id="back-conv")
    assert state.read_session(tmp_path, T, N, repo=BACK).session_id == "back-conv"
    assert state.has_waiting(tmp_path, T, N, repo=BACK)
    assert state.read_session(tmp_path, T, N, repo=FRONT) is None


def test_status_sidecars_are_per_repository_and_survive_restart(tmp_path, monkeypatch):
    Herdr(monkeypatch, [FRONT_LABEL, BACK_LABEL])
    assert Sessions(state_dir=tmp_path).idle_seconds(T, N, repo=FRONT) is not None
    side = tmp_path / "herdr-status"
    assert (side / f"{FRONT_LABEL}.json").exists()
    assert not (side / f"{BACK_LABEL}.json").exists()
    Sessions(state_dir=tmp_path).idle_seconds(T, N, repo=BACK)
    # A restarted dispatcher continues the FRONT clock instead of starting afresh.
    path = side / f"{FRONT_LABEL}.json"
    rec = json.loads(path.read_text())
    rec["since"] = time.time() - 600
    path.write_text(json.dumps(rec))
    assert Sessions(state_dir=tmp_path).idle_seconds(T, N, repo=FRONT) > 500
    Sessions(state_dir=tmp_path).forget_status(T, N, repo=BACK)
    assert path.exists() and not (side / f"{BACK_LABEL}.json").exists()


def test_proven_live_legacy_session_stays_addressable(tmp_path, monkeypatch, podman):
    server = Herdr(monkeypatch, [LEGACY_LABEL])
    state.record_session_owner(tmp_path, T, N, FRONT)
    s = Sessions(state_dir=tmp_path)
    assert s.is_alive(T, N, repo=FRONT) is True
    s.end(T, N, repo=FRONT)
    assert server.closed() == ["w1:t2"]
    assert LEGACY_LABEL in removed(podman)
    # Owning `task-shop-12` never authorizes removing the globally shared `task-12`.
    assert "task-12" not in removed(podman)
    assert (tmp_path / "snapshots" / f"task-{state.task_key(T, N, FRONT)}.txt").exists()


def test_legacy_session_is_not_borrowed_by_the_other_repository(tmp_path, monkeypatch, podman):
    server = Herdr(monkeypatch, [LEGACY_LABEL])
    state.record_session_owner(tmp_path, T, N, FRONT)
    s = Sessions(state_dir=tmp_path)
    assert s.is_alive(T, N, repo=BACK) is False
    s.end(T, N, repo=BACK)
    assert server.closed() == []
    assert LEGACY_LABEL not in removed(podman)
    assert "task-12" not in removed(podman)


def test_prune_keeps_the_qualified_snapshot_of_an_in_flight_task(tmp_path):
    from dispatcher.main import SNAPSHOT_TTL_SECONDS, _prune_snapshots
    state.save(tmp_path, make_task(issue=N, target=T, repo=FRONT))
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    old = time.time() - SNAPSHOT_TTL_SECONDS - 3600
    for key in (state.task_key(T, N, FRONT), state.task_key(T, N, BACK)):
        p = snaps / f"task-{key}.txt"
        p.write_text("x")
        os.utime(p, (old, old))
    _prune_snapshots(make_config(tmp_path))
    assert (snaps / f"task-{state.task_key(T, N, FRONT)}.txt").exists()
    assert not (snaps / f"task-{state.task_key(T, N, BACK)}.txt").exists()


def test_launched_executions_stop_hook_pings_with_its_trusted_identity(
        tmp_path, monkeypatch, podman):
    """Launch BACK, fire the hook the launch installed, deliver its ping."""
    wt = tmp_path / "wt"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: elsewhere\n")
    # What provisioning writes today: no repository in it.
    (wt / ".agent" / "task.json").write_text(
        json.dumps({"issue": N, "target": T, "branch": f"agent/task-{N}"}))
    Herdr(monkeypatch, [FRONT_LABEL, BACK_LABEL])
    clone = tmp_path / "clone"
    clone.mkdir()
    _save_both(tmp_path)
    Sessions(state_dir=tmp_path, clones={T: str(clone)}).spawn_stage(
        T, N, str(wt), "p", "implement", "claude-opus-5", repo=BACK)
    bin_dir, sent = tmp_path / "bin", tmp_path / "sent.json"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do\n'
                    '  [ "$1" = -d ] && printf %s "$2" > "$SENT"\n  shift\ndone\n')
    curl.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SENT": str(sent),
           "AGENT_OPS_STATE_DIR": str(tmp_path)}
    sp.run([str(wt / ".agent" / "stop-hook.sh")], input=json.dumps({"session_id": "back-conv"}),
           text=True, env=env, check=True)
    handle_ping(sent.read_bytes(), tmp_path)
    assert state.read_session(tmp_path, T, N, repo=BACK).session_id == "back-conv"
    assert state.has_waiting(tmp_path, T, N, repo=BACK)
    assert state.read_session(tmp_path, T, N, repo=FRONT) is None
    assert not state.has_waiting(tmp_path, T, N, repo=FRONT)


def test_tokenless_execution_rejects_declarations_once_a_sibling_is_launched(tmp_path):
    _save_both(tmp_path)
    state.issue_execution_token(tmp_path, T, N, FRONT)  # BACK keeps no token
    _ping(tmp_path, repo=BACK, session_id="forged")
    _ping(tmp_path, repo=BACK, background_tasks=[{"id": "b1"}])
    for repo in (FRONT, BACK, ""):
        assert state.read_session(tmp_path, T, N, repo=repo) is None
        assert not state.has_waiting(tmp_path, T, N, repo=repo)
        assert state.read_background(tmp_path, T, N, repo=repo) is None


def test_legacy_stop_ping_without_repo_maps_to_the_recorded_owner(tmp_path):
    _save_both(tmp_path)
    state.record_session_owner(tmp_path, T, N, FRONT)
    _ping(tmp_path, session_id="legacy-conv")  # a hook from before qualification
    assert state.read_session(tmp_path, T, N, repo=FRONT).session_id == "legacy-conv"
    assert state.has_waiting(tmp_path, T, N, repo=FRONT)
    assert state.read_session(tmp_path, T, N, repo=BACK) is None
    assert not state.has_waiting(tmp_path, T, N, repo=BACK)
    assert not state.has_waiting(tmp_path, T, N)


def test_legacy_ping_rejects_a_token_issued_to_another_execution(tmp_path):
    _save_both(tmp_path)
    state.record_session_owner(tmp_path, T, N, FRONT)
    back_token = state.issue_execution_token(tmp_path, T, N, BACK)
    _ping(tmp_path, token=back_token, session_id="forged")
    _ping(tmp_path, token=back_token, background_tasks=[{"id": "b1"}])
    for repo in (FRONT, BACK, ""):
        assert state.read_session(tmp_path, T, N, repo=repo) is None
        assert not state.has_waiting(tmp_path, T, N, repo=repo)
        assert state.read_background(tmp_path, T, N, repo=repo) is None


def test_legacy_ping_still_continues_after_a_sibling_is_launched(tmp_path):
    _save_both(tmp_path)
    state.record_session_owner(tmp_path, T, N, FRONT)
    state.issue_execution_token(tmp_path, T, N, BACK)  # backend launched later
    _ping(tmp_path, session_id="legacy-conv")
    assert state.read_session(tmp_path, T, N, repo=FRONT).session_id == "legacy-conv"
    assert state.has_waiting(tmp_path, T, N, repo=FRONT)
    assert state.read_session(tmp_path, T, N, repo=BACK) is None
    assert not state.has_waiting(tmp_path, T, N, repo=BACK)


def test_migration_with_a_verified_owner_never_touches_the_shared_bare_name(
        tmp_path, monkeypatch):
    from dispatcher import tmux_migration
    state.save(tmp_path, make_task(issue=N, target=T, repo=FRONT))
    state.record_session_owner(tmp_path, T, N, FRONT)
    acts = []
    monkeypatch.setattr(tmux_migration, "_has_session",
                        lambda n: n in (LEGACY_LABEL, "task-12"))
    monkeypatch.setattr(tmux_migration, "_kill_session", lambda n: acts.append(("kill", n)) or n)
    monkeypatch.setattr(tmux_migration, "_remove_container", lambda n: acts.append(("rm", n)) or n)
    tmux_migration.migrate(tmp_path, lambda task, msg: None)
    assert ("kill", LEGACY_LABEL) in acts and ("rm", LEGACY_LABEL) in acts
    assert all(name != "task-12" for _, name in acts)


def test_flushing_one_repositorys_finished_task_leaves_its_sibling_intact(
        tmp_path, monkeypatch):
    from dataclasses import replace
    from dispatcher import main
    from dispatcher.config import Project, ProjectRepository
    from tests.test_main import cfg
    members = [ProjectRepository(
        repo=r, clone_path=str(tmp_path / f"{n}-clone"), worktrees_path=str(tmp_path / f"{n}-wt"),
        rank_cmd="rank", gate_cmd="make gate") for r, n in ((FRONT, "front"), (BACK, "back"))]
    project = Project(key="shop", name="Shop", board_owner="example", board_number=1,
                      status_field_id="F", status_ready_option_id="R",
                      status_in_progress_option_id="I", repositories=members)
    c = replace(cfg(tmp_path), targets=[], projects=[project], done_retention_days=0)
    wt = str(tmp_path / "back-wt" / "task-12")
    state.save(c.state_dir, make_task(
        issue=N, target="shop", repo=BACK, stage=state.Stage.FAILED, worktree=wt,
        branch="agent/task-12", updated_at="2000-01-01T00:00:00+00:00"))
    state.save(c.state_dir, make_task(
        issue=N, target="shop", repo=FRONT, stage=state.Stage.IMPLEMENT,
        worktree=str(tmp_path / "front-wt" / "task-12"), branch="agent/task-12"))
    calls = []
    monkeypatch.setattr(main, "remove_workspace",
                        lambda target, wt, branch, dry_run=False: calls.append(target.clone_path))
    main._flush_finished(c)
    assert calls == [members[1].clone_path]
    assert [t.repo for t in state.load_all(c.state_dir)] == [FRONT]


def test_distinct_dotted_identities_never_share_an_execution_label():
    """Repository and target names may hold dots: `example/a.b` + `c` and
    `example/a` + `b.c` are two executions, so two tabs and two containers."""
    one = sessions.session_name("c", N, "example/a.b")
    other = sessions.session_name("b.c", N, "example/a")
    assert one != other
    for label in (one, other):
        assert PODMAN_NAME.fullmatch(label), f"Podman rejects --name {label}"


def test_colliding_dotted_identities_end_only_their_own_session(tmp_path, monkeypatch, podman):
    one = sessions.session_name("c", N, "example/a.b")
    other = sessions.session_name("b.c", N, "example/a")
    server = Herdr(monkeypatch, [one], workspaces=("b.c", "c"))
    Sessions(state_dir=tmp_path).end("b.c", N, repo="example/a")
    assert server.closed() == []
    assert one not in removed(podman)


def test_explicit_declaration_without_any_token_writes_nothing(tmp_path):
    """No execution token and no launch record exists anywhere: a declared
    repository still proves nothing, whatever its issue number."""
    _save_both(tmp_path)
    _ping(tmp_path, repo=BACK, session_id="forged")
    _ping(tmp_path, repo=BACK, background_tasks=[{"id": "b1"}])
    for repo in (FRONT, BACK, ""):
        assert state.read_session(tmp_path, T, N, repo=repo) is None
        assert not state.has_waiting(tmp_path, T, N, repo=repo)
        assert state.read_background(tmp_path, T, N, repo=repo) is None


def test_tokenless_destination_rejects_a_token_borrowed_from_another_issue(tmp_path):
    _save_both(tmp_path)
    state.save(tmp_path, make_task(issue=N + 1, target=T, repo=FRONT))
    other_issue = state.issue_execution_token(tmp_path, T, N + 1, FRONT)
    _ping(tmp_path, repo=BACK, token=other_issue, session_id="forged")
    for repo in (FRONT, BACK, ""):
        assert state.read_session(tmp_path, T, N, repo=repo) is None
        assert not state.has_waiting(tmp_path, T, N, repo=repo)


def test_expired_project_only_task_removes_its_member_worktree(tmp_path, monkeypatch):
    from dataclasses import replace
    from dispatcher import main
    from dispatcher.config import Project, ProjectRepository
    from tests.test_main import cfg
    member = ProjectRepository(
        repo=BACK, clone_path=str(tmp_path / "back-clone"),
        worktrees_path=str(tmp_path / "back-wt"), rank_cmd="rank", gate_cmd="make gate")
    project = Project(key="shop", name="Shop", board_owner="example", board_number=1,
                      status_field_id="F", status_ready_option_id="R",
                      status_in_progress_option_id="I", repositories=[member])
    c = replace(cfg(tmp_path), targets=[], projects=[project], done_retention_days=0)
    wt = str(tmp_path / "back-wt" / "task-12")
    state.save(c.state_dir, make_task(
        issue=N, target="shop", repo=BACK, stage=state.Stage.DONE, worktree=wt,
        branch="agent/task-12", updated_at="2000-01-01T00:00:00+00:00"))
    calls = []
    monkeypatch.setattr(main, "remove_workspace",
                        lambda target, wt, branch, dry_run=False:
                        calls.append((target.clone_path, wt, branch)))
    main._flush_finished(c)
    assert calls == [(member.clone_path, wt, "agent/task-12")]
    assert state.load_all(c.state_dir) == []


def _git(cwd, *args):
    sp.run(["git", *args], cwd=cwd, check=True, capture_output=True,
           env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _target_with_clone(tmp_path, name):
    from tests.webfakes import make_target
    origin = tmp_path / f"{name}-origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "f").write_text(name)
    _git(origin, "add", "f")
    _git(origin, "commit", "-q", "-m", "init")
    clone = tmp_path / f"{name}-clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    return _retarget(make_target(name=name, repo=f"example/{name}"), clone,
                     tmp_path / f"{name}-wt")


def _retarget(target, clone, worktrees):
    from dataclasses import replace
    return replace(target, clone_path=str(clone), worktrees_path=str(worktrees))


def _branches(clone):
    return sp.run(["git", "branch", "--list", "agent/*", "--format=%(refname:short)"], cwd=clone, text=True,
                  capture_output=True).stdout.split()


def test_same_number_worktrees_and_branches_belong_to_their_own_repository(tmp_path):
    from dispatcher import workspace
    front = _target_with_clone(tmp_path, "front")
    back = _target_with_clone(tmp_path, "back")
    wt_front = workspace.create_workspace(front, N)
    wt_back = workspace.create_workspace(back, N)
    assert wt_front != wt_back and Path(wt_front).is_dir() and Path(wt_back).is_dir()
    assert _branches(front.clone_path) == _branches(back.clone_path) == ["agent/task-12"]
    workspace.remove_workspace(back, wt_back, "agent/task-12")
    assert not Path(wt_back).exists() and _branches(back.clone_path) == []
    assert Path(wt_front).is_dir() and _branches(front.clone_path) == ["agent/task-12"]


def test_migration_without_an_ownership_record_leaves_a_live_shared_session(
        tmp_path, monkeypatch):
    """With the sibling gone from the inventory, a missing launch record
    still proves nothing about who owns `task-shop-12`."""
    from dispatcher import tmux_migration
    state.save(tmp_path, make_task(issue=N, target=T, repo=BACK))
    acts = []
    monkeypatch.setattr(tmux_migration, "_has_session", lambda n: n == LEGACY_LABEL)
    monkeypatch.setattr(tmux_migration, "_kill_session", lambda n: acts.append(("kill", n)) or n)
    monkeypatch.setattr(tmux_migration, "_remove_container", lambda n: acts.append(("rm", n)) or n)
    woken = []
    tmux_migration.migrate(tmp_path, lambda task, msg: woken.append(task.repo))
    assert acts == [] and woken == []


def _launch_worktree(tmp_path, task_json=None):
    wt = tmp_path / "wt"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: elsewhere\n")
    if task_json is not None:
        (wt / ".agent" / "task.json").write_text(json.dumps(task_json))
    return wt


def test_launch_mounts_the_clone_of_its_own_repository(tmp_path, monkeypatch, podman):
    wt = _launch_worktree(tmp_path)
    server = Herdr(monkeypatch, [BACK_LABEL])
    front, back = tmp_path / "front", tmp_path / "back"
    front.mkdir()
    back.mkdir()
    Sessions(state_dir=tmp_path, clones={T: str(front), BACK: str(back)}).spawn_stage(
        T, N, str(wt), "p", "implement", "claude-opus-5", repo=BACK)
    run = server.launched()[0][3]
    assert f"{back}:{back}" in run and str(front) not in run


@pytest.mark.parametrize("recorded, injected", [
    ("agent/task-12", True),
    ("agent/task-99", False),
])
def test_launch_injects_only_the_branch_of_its_own_issue(
        tmp_path, monkeypatch, podman, recorded, injected):
    wt = _launch_worktree(tmp_path, {"branch": recorded})
    server = Herdr(monkeypatch, [BACK_LABEL])
    clone = tmp_path / "clone"
    clone.mkdir()
    Sessions(state_dir=tmp_path, clones={T: str(clone)}).spawn_stage(
        T, N, str(wt), "p", "implement", "claude-opus-5", repo=BACK)
    run = server.launched()[0][3]
    assert (f"AGENT_OPS_TASK_BRANCH={recorded}" in run) is injected
