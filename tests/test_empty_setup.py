"""Ticket 06 acceptance: claiming a candidate for a target with no
`setup_cmd` must not run a provisioning container and must not be treated
as a provisioning failure — the task lands in the spec stage exactly like
a target that does provision. Exercises the REAL create_workspace (not the
patch_workspace fake other test_main.py tests use), with only git
subprocess calls faked, so the empty-setup_cmd skip is actually observed.
"""
from dataclasses import replace as dc_replace
from pathlib import Path

import dispatcher.workspace as workspace
from dispatcher import failures
from dispatcher.config import Config, Target
from dispatcher.github import Candidate
from dispatcher.state import Stage, load

from tests.test_main import FakeGitHub, FakeSessions, FakeNotifier, deps, patch_usage


def cfg_no_setup(tmp_path: Path) -> Config:
    return Config(
        state_dir=str(tmp_path / "state"), capacity=3,
        session_memory="2g", session_cpus="2",
        targets=[Target(
            name="factorial", repo="jesdi/factorial",
            clone_path=str(tmp_path / "repo"),
            worktrees_path=str(tmp_path / "repo.worktrees"),
            rank_cmd="rank", setup_cmd="",
            verify_cmd="make e2e-slot SLOT={slot}",
            gate_cmd="make gate",
            project_number=1, project_owner="jesdi",
            status_field_id="F", status_ready_option_id="R",
            status_in_progress_option_id="I",
        )],
        infra_repo="jesdi/agent-ops",
    )


def fake_git_sh(tmp_path):
    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append(args)
        if "worktree" in args:  # simulate git actually creating the dir
            wt = Path(args[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").write_text(
                f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")

    return calls, fake_sh


def test_claim_with_empty_setup_cmd_runs_no_container_and_reaches_spec(
        tmp_path, monkeypatch):
    calls, fake_sh = fake_git_sh(tmp_path)
    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    patch_usage(monkeypatch)

    c = cfg_no_setup(tmp_path)
    gh = FakeGitHub([Candidate(42, "Compute factorial", "u42")])
    sess = FakeSessions()
    notifier = FakeNotifier()

    import dispatcher.main as main
    main.run_pass(c, deps(gh, sess, notifier))

    assert not any(a[0] == "podman" for a in calls), \
        "empty setup_cmd must not run a provisioning container"
    assert gh.claimed == [42]
    ts = load(c.state_dir, "factorial", 42)
    assert ts.stage is Stage.SPEC
    assert [s[:2] for s in sess.spawned] == [(42, "spec")]

    # No provisioning-failure issue, ping, or quarantine record.
    assert gh.created_issues == []
    assert "provisioning_failed" not in notifier.sent
    assert not failures.quarantine_path(c.state_dir, "factorial", 42).exists()
