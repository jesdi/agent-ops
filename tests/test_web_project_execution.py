"""Project execution controls use the task repository's policy, even paused."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from dispatcher import execution_overrides, intents, state
from dispatcher.config import Project, ProjectRepository
from dispatcher.models import parse_policy
from tests.test_web_qualified_task_api import FakeGitHub, FakeSessions, row
from tests.webfakes import HEADERS, make_config, make_task
from web.app import create_app
from web.sources import Sources

PROJECT = "billing"
FIRST, SECOND = "example/billing-api", "example/billing-web"
OVERRIDE = "anthropic/claude-sonnet-4-6"
INHERITED = "anthropic/claude-opus-5"


def rig(tmp_path, *, claimed=True):
    project = Project(
        key=PROJECT, name="Billing", board_owner="example", board_number=1,
        status_field_id="F", status_ready_option_id="R",
        status_in_progress_option_id="P", repositories=[
            ProjectRepository(repo=repo, clone_path="/tmp/clone",
                              worktrees_path="/tmp/worktrees", rank_cmd="false",
                              gate_cmd="true", models=policy)
            for repo, policy in [(FIRST, None), (SECOND, parse_policy({
                "triage": [OVERRIDE], "untracked": "standard",
                "tracks": {"standard": {"when": "Everything.", **{
                    stage: [OVERRIDE] for stage in ("spec", "plan", "implement", "review")
                }}}
            }))]])
    cfg = replace(make_config(tmp_path), targets=[], projects=[project])
    if claimed:
        for repo in (FIRST, SECOND):
            state.save(tmp_path, make_task(issue=12, target=PROJECT, repo=repo,
                                          park=state.PARK_HUMAN))

    class GitHub(FakeGitHub):
        def rank_rows(self, target):
            # A cold queue read must rank every member of the project.
            assert isinstance(target, Project)
            return [row(FIRST), row(SECOND)]

    sources = Sources(str(tmp_path), FakeSessions(), GitHub(), systemctl=("true",))
    return TestClient(create_app(cfg, sources))


def url(repo, action):
    return f"/api/task/{PROJECT}/{repo.replace('/', '~')}/12/{action}"


@pytest.mark.parametrize("action", ["resume", "run"])
@pytest.mark.parametrize("repo,model", [(FIRST, INHERITED), (SECOND, OVERRIDE)])
def test_paused_project_controls_use_repository_policy(tmp_path, action, repo, model):
    client = rig(tmp_path)
    response = client.post(url(repo, action), headers=HEADERS, json={"model": model})
    assert response.status_code == 202, response.text
    pending = intents.list_intents(tmp_path)
    assert len(pending) == 1
    assert (pending[0].target, pending[0].repo, pending[0].payload["model"]) == (
        PROJECT, repo, model)


@pytest.mark.parametrize("action", ["resume", "run"])
@pytest.mark.parametrize("repo,model", [(FIRST, OVERRIDE), (SECOND, INHERITED)])
def test_project_controls_reject_sibling_repository_policy(tmp_path, action, repo, model):
    client = rig(tmp_path)
    response = client.post(url(repo, action), headers=HEADERS, json={"model": model})
    assert response.status_code == 422, response.text
    assert not intents.list_intents(tmp_path)


@pytest.mark.parametrize("claimed", [True, False])
def test_project_run_defaults_to_repository_model(tmp_path, claimed):
    client = rig(tmp_path, claimed=claimed)
    response = client.post(url(SECOND, "run"), headers=HEADERS, json={})
    assert response.status_code == (202 if claimed else 200), response.text
    if claimed:
        assert intents.list_intents(tmp_path)[0].payload["model"] == OVERRIDE
    else:
        override = execution_overrides.load(tmp_path, PROJECT, 12, repo=SECOND)
        assert override.model == OVERRIDE
        assert execution_overrides.load(tmp_path, PROJECT, 12, repo=FIRST) is None


def test_project_run_refuses_ambiguous_issue_number(tmp_path):
    client = rig(tmp_path, claimed=False)
    response = client.post(f"/api/task/{PROJECT}/12/run", headers=HEADERS, json={})
    assert response.status_code == 409, response.text
