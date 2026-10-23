"""Board-choice corrections through the operator-terminal proposal seam."""
import io
from copy import deepcopy

import pytest

from project_cli import main

REPO = "example/analytics"


class Box:
    def __init__(self, board=None):
        self.board = board
        self.writes = []

    def projects(self):
        return {}

    def inspect(self, repo):
        return dict(setup_cmd="make setup", verify_cmd="make e2e",
                    gate_cmd="make gate", board=self.board)

    def confirm(self, plan):
        self.writes.append(deepcopy(plan))


def run(box, answers):
    out, err = io.StringIO(), io.StringIO()
    code = main(["project", "add", "analytics", REPO],
                stdin=io.StringIO(answers), stdout=out, stderr=err, box=box)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("initial", [None, {"owner": "unsuitable", "number": 1}])
@pytest.mark.parametrize("choice, expected, display", [
    ("example/42", {"owner": "example", "number": 42}, "example/42"),
    ("create:example/Analytics backlog", {"owner": "example", "create": True,
                                          "title": "Analytics backlog"},
     "create example/Analytics backlog"),
])
def test_operator_can_select_or_create_board_before_confirming(initial, choice, expected, display):
    box = Box(initial)
    code, out, err = run(box, f"edit board={choice}\nyes\n")
    assert code == 0
    assert not err
    [plan] = box.writes
    assert plan["board"] == expected
    assert display in out
    revisions = [line for line in out.splitlines() if line.startswith("Revision:")]
    assert len(set(revisions)) == 2
    assert plan["revision"] == revisions[-1].split()[-1]
    assert "board: temporary item update, then restore it" in plan["write_tests"]


@pytest.mark.parametrize("answer", ["no\n", ""])
def test_changed_board_needs_fresh_confirmation(answer):
    box = Box()
    _, out, _ = run(box, "edit board=create:example/Analytics\n" + answer)
    assert "create example/Analytics" in out
    assert box.writes == []


def test_unresolved_board_cannot_be_confirmed():
    box = Box()
    _, _, err = run(box, "yes\n")
    assert "board" in err.lower()
    assert box.writes == []


@pytest.mark.parametrize("choice", ["example/0", "example/nope", "create:/Title", "create:example/", ""])
def test_invalid_board_choice_preserves_displayed_plan(choice):
    board = {"owner": "example", "number": 7}
    box = Box(board)
    _, _, err = run(box, f"edit board={choice}\nyes\n")
    assert "board" in err.lower()
    assert box.writes[0]["board"] == board
