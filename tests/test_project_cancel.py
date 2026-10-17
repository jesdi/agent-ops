from dataclasses import replace

import pytest

import dispatcher.main as main
from dispatcher import eventlog, intents
from dispatcher.config import Project, ProjectRepository
from tests.test_main import (FakeGitHub, FakeSessions, cfg, deps, patch_usage,
                             patch_workspace)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("repo,issue,accepted", [
    ("example/api", 12, True),
    ("example/web", 12, True),
    ("example/web", 99, False),
    ("example/other", 12, False),
])
def test_cancel_unclaimed_project_issue(tmp_path, monkeypatch, enabled,
                                        repo, issue, accepted):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    project = Project(
        key="shop", name="Shop", board_owner="example", board_number=1,
        status_field_id="F", status_ready_option_id="R",
        status_in_progress_option_id="I", enabled=enabled,
        repositories=[ProjectRepository(
            repo=f"example/{name}", clone_path=str(tmp_path / name),
            worktrees_path=str(tmp_path / f"{name}.wt"), rank_cmd="rank",
            gate_cmd="make gate") for name in ("api", "web")])
    config = replace(cfg(tmp_path), targets=[], projects=[project])

    class QueueGitHub(FakeGitHub):
        def rank_rows(self, target):
            # Match the GitHub client's project-wide versus single-repo scope.
            repos = ([member.repo for member in target.repositories]
                     if isinstance(target, Project) else [target.repo])
            return [{"repo": member, "number": 12} for member in repos]

        def cancel(self, target, number):
            self.canceled.append((target.repo, number))

    gh = QueueGitHub()
    intents.write_intent(config.state_dir, "cancel", "shop", issue,
                         {}, "op", 1, repo=repo)
    main.run_pass(config, deps(gh, FakeSessions()))

    assert gh.canceled == ([(repo, issue)] if accepted else [])
    events = eventlog.read_tail(config.state_dir)
    assert any(event["event"] == ("intent-applied" if accepted else "intent-dropped")
               and event["repo"] == repo for event in events)
