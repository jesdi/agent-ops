"""Review regressions through CLI selection, publication and future reconciliation."""
import json
import subprocess

import pytest

from dispatcher import board
from tests.test_board import SCHEMA, _issue, _page
from tests.test_project_board_onboarding_acceptance import (
    Box, FIELDS, REPO, REF, WorldBoards, _confirmed, _control, board as state, card, repo_info,
)


class InspectableBoards(WorldBoards):
    def board_state(self, ref):
        live = self._board(ref)
        return {**live, "fields": [{**f, "id": f"F_{f['name']}"} for f in live["fields"]], "cards": live["cards"]}


def world(cards=()):
    return InspectableBoards([{**REF, "id": "PVT_7", "title": "T", "fields": FIELDS,
                               "cards": list(cards)}])


def test_membership_changes_after_cli_confirmation_block_publication(tmp_path):
    plan = _confirmed(Box({REPO: repo_info(state())}), "yes\n")
    boards = world([card("example/billing", 4)])
    control, github = _control(tmp_path, boards)
    op = control.confirm(plan)
    control.advance(op)
    assert control.status(op)["phase"] == "blocked"
    assert "example/billing" in control.status(op)["blocker"]
    assert "analytics" not in github.config["projects"]


def test_new_board_proposes_a_scorable_area():
    plan = _confirmed(Box({REPO: repo_info(ref=None)}), "edit board=create:example/Backlog\nyes\n")
    area = next(f for f in plan["board_prep"]["fields"] if f["name"] == "Area")
    assert area.get("options")


def test_selected_board_overrides_old_clone_and_imports_old_reopened_issue(tmp_path):
    from dispatcher import triage
    from dispatcher.config import Config, Project, ProjectRepository

    (tmp_path / ".backlog").mkdir()
    (tmp_path / board.META_PATH).write_text(json.dumps(SCHEMA))
    calls, mutations = [], []
    fields = [{"name": name, **f, "options": [{"name": n, "id": i} for n, i in f.get("options", {}).items()]}
              for name, f in SCHEMA["fields"].items()]

    def run(args, **kwargs):
        calls.append(args)
        query = next((a for a in args if a.startswith("query=")), "")
        if "mutation" in query:
            mutations.append(args)
            out = '{}'
        elif "repositoryOwner" in query:
            out = json.dumps({"data": {"repositoryOwner": {"projectV2": {"id": "PVT_9"}}}})
        elif args[1:3] == ["project", "field-list"]:
            out = json.dumps({"fields": fields})
        elif args[1:3] == ["api", "graphql"]:
            out = _page([])
        elif args[1:3] == ["issue", "list"]:
            out = json.dumps([_issue(n, state="CLOSED") for n in range(2, 1002)])
        elif args[1] == "api":
            assert "--paginate" in args and "--slurp" in args
            assert kwargs.get("env") is None  # repository auth, never the board credential
            out = json.dumps([[{"number": 1, "title": "reopened", "state": "open"},
                               {"number": 2000, "title": "PR", "pull_request": {}}],
                              [{"number": 1002, "title": "new", "state": "open"}]])
        else:
            out = json.dumps({"id": "ISSUE"})
        return subprocess.CompletedProcess(args, 0, out, "")

    class Credentials:
        def acquire(self, principal, key, purpose):
            assert key == "example/9" and purpose == "board"
            return type("Lease", (), {"token": "board-token"})()

    project = Project(key="p", name="p", board_owner="example", board_number=9,
                      status_field_id="F_STATUS", status_ready_option_id="o_ready",
                      status_in_progress_option_id="o_prog",
                      repositories=[ProjectRepository(repo=REPO, clone_path=str(tmp_path),
                        worktrees_path="/w", rank_cmd="rank", gate_cmd="gate")])
    lines = []
    brd = triage._board(Config(state_dir=str(tmp_path), capacity=2, session_memory="1g",
                               session_cpus="1", targets=[], projects=[project]), REPO, lines, run, Credentials())
    assert brd is not None, lines
    assert brd.schema.project_id == "PVT_9"
    assert len(mutations) == 2
    assert all("p=PVT_9" in args for args in mutations)


@pytest.mark.parametrize("corruption, detail", [
    ({"cards": [{"repo": None, "number": None}]}, "unresolved"),
    ({"id": "OTHER"}, "identity"),
    ({"fields": [{**f, "type": "text"} if f["name"] == "Impact" else f for f in FIELDS]}, "Impact"),
])
def test_direct_confirmation_revalidates_identity_membership_and_schema(tmp_path, corruption, detail):
    plan = _confirmed(Box({REPO: repo_info(state())}), "yes\n")
    plan["board"]["id"] = "PVT_7"
    boards = world()
    boards.boards[0].update(corruption)
    control, github = _control(tmp_path, boards)
    op = control.confirm(plan)
    control.advance(op)
    assert control.status(op)["phase"] == "blocked"
    assert detail in control.status(op)["blocker"]
    assert "analytics" not in github.config["projects"]


def test_a_completed_preparation_does_not_bypass_validation_on_retry(tmp_path):
    plan = _confirmed(Box({REPO: repo_info(state())}), "yes\n")
    boards = world()
    control, github = _control(tmp_path, boards)
    # Keep the configuration PR pending while membership changes.
    github.checks = lambda number: {"state": "pending", "failing": []}
    op = control.confirm(plan)
    control.advance(op)
    assert control.status(op)["phase"] == "published"
    boards.boards[0]["cards"].append(card("example/billing", 4))
    github.checks = lambda number: {"state": "passing", "failing": []}
    control.advance(op)
    assert control.status(op)["phase"] == "blocked"
    assert "analytics" not in github.config["projects"]


def test_creation_recovers_and_prepares_a_usable_board(tmp_path):
    plan = _confirmed(Box({REPO: repo_info(ref=None)}), "edit board=create:example/Backlog\nyes\n")
    boards = InspectableBoards(lose_create=True)
    control, github = _control(tmp_path, boards)
    op = control.confirm(plan)
    control.advance(op)
    assert control.status(op)["phase"] == "blocked"
    control.advance(op)
    assert control.status(op)["phase"] == "complete", control.status(op)
    assert len(boards.boards) == 1
    published = github.config["projects"]["analytics"]["board"]
    assert published["id"] == boards.boards[0]["id"]
    assert published["fields"]["Area"]
    assert next(f for f in boards.boards[0]["fields"] if f["name"] == "Area")["options"]
