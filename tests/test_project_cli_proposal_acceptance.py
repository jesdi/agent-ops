"""Acceptance tests for ticket 14 (project-onboarding): the operator-terminal
proposal of `agent-ops project add`, driven through the PROJECT_CLI seam.

Spec: specs/project-onboarding/spec.md S01, S02, S51 (R1, R10, R12).
Ticket: .agent/tickets/14-local-onboarding-proposal.md.

Interface these tests define (the ticket leaves its shape open):
- `project_cli.main(argv, *, stdin, stdout, stderr, box) -> int` is what the
  `agent-ops` console script calls; `argv` starts after the program name, e.g.
  `["project", "add", "analytics", "example/analytics"]`. Missing name or
  repositories are prompted for. Exit code 0 = ok (including a declined
  proposal), non-zero = refused.
- `box` is the transport to the box (SSH in production, a double here). Reads:
  `box.projects() -> {project name: [repository, ...]}` (registered projects)
  and `box.inspect(repo) -> dict` with optional keys `setup_cmd`, `verify_cmd`,
  `gate_cmd`, `board` ({"owner", "number"}), `compose_services` (list) and
  `secret_refs` (list of 1Password references). An absent key is information
  the repository does not declare. Writes: `box.confirm(plan: dict)` starts the
  durable operation and `box.resume(name)` resumes an existing project's.
  Those two are the only calls that may change anything.
- The proposal is printed to stdout in sections headed `Configuration:`,
  `Access:`, `Services:`, `Secret references:`, `Write tests:` and `Missing:`
  (the last only when something is absent), then `Revision: <id>`, then the
  prompt. The operator answers on stdin: `yes` confirms the displayed revision,
  `edit <repo>.<field>=<value>` changes a field and reprints the proposal under
  a new revision, anything else (or EOF) declines.
- The plan handed to `box.confirm` is a dict with `name`, `revision`,
  `repositories` (dicts with `repo`, `setup_cmd`, `verify_cmd`, `gate_cmd`,
  `compose_services`), `board`, `models` ("inherit" unless overridden),
  `access`, `secret_refs` and `write_tests`.
- A repository registered under another project is found by
  `dispatcher.config.normalize_repo` identity, so HTTPS/SSH spellings match.
- `agent-ops` in pyproject.toml's `[project.scripts]` names a callable in the
  `project_cli` package.
"""
import io
import tomllib
from copy import deepcopy
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO = "example/analytics"
INSPECTED = {
    REPO: dict(setup_cmd="pnpm install --frozen-lockfile", verify_cmd="pnpm e2e",
               gate_cmd="make gate", board=dict(owner="example", number=7),
               compose_services=["postgres", "redis"],
               secret_refs=["op://Analytics/db/password"]),
    "example/analytics-web": dict(setup_cmd="npm ci", verify_cmd="npm run e2e",
                                  gate_cmd="npm test", board=dict(owner="example", number=7),
                                  compose_services=[], secret_refs=[]),
}


class Box:
    """The box over SSH: serves reads, records every other call as a write."""

    def __init__(self, registered=None, inspected=INSPECTED):
        self.registered = dict(registered or {})
        self.inspected = inspected
        self.reads = []
        self.writes = []

    def projects(self):
        self.reads.append("projects")
        return {k: list(v) for k, v in self.registered.items()}

    def inspect(self, repo):
        self.reads.append(("inspect", repo))
        return dict(self.inspected.get(repo, {}))

    def board_state(self, ref):
        self.reads.append(("board_state", ref))
        return dict(cards=[], fields=[
            dict(name="Status", type="single_select", options=["Inbox", "Ready", "In progress"]),
            *(dict(name=n, type=t) for n, t in (("Impact", "number"), ("Effort", "number"),
                                                ("Score", "number"), ("Area", "single_select")))])

    def confirm(self, plan):
        self.writes.append(("confirm", plan))
        return "op-1"

    def resume(self, name):
        self.writes.append(("resume", name))
        return "op-1"

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **kw: self.writes.append((name, a, kw))


def run(box, argv, stdin=""):
    import project_cli
    out, err = io.StringIO(), io.StringIO()
    code = project_cli.main(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, box=box)
    return code, out.getvalue(), err.getvalue()


def section(text, heading):
    """Lines under `heading` up to the next blank line."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading) + 1
    body = []
    for line in lines[start:]:
        if not line.strip():
            break
        body.append(line)
    return "\n".join(body)


def revisions(text):
    return [line.split(":", 1)[1].strip() for line in text.splitlines()
            if line.startswith("Revision:")]


def test_add_proposes_every_discovered_setting_before_applying_anything():
    box = Box()
    code, out, _ = run(box, ["project", "add", "analytics", REPO], stdin="no\n")
    assert code == 0
    assert box.writes == []
    assert "analytics" in section(out, "Configuration:")
    config = section(out, "Configuration:")
    for expected in (REPO, "pnpm install --frozen-lockfile", "pnpm e2e", "make gate",
                     "example", "7", "inherit"):
        assert expected in config, expected
    assert "postgres" in section(out, "Services:") and "redis" in section(out, "Services:")
    assert "op://Analytics/db/password" in section(out, "Secret references:")
    assert REPO in section(out, "Access:")
    write_tests = section(out, "Write tests:")
    assert REPO in write_tests
    assert "temporary" in write_tests.lower() and "branch" in write_tests.lower()
    assert "pull request" in write_tests.lower()
    assert "board" in write_tests.lower() and "restore" in write_tests.lower()
    assert revisions(out)


def test_confirming_applies_exactly_the_displayed_plan():
    box = Box()
    code, out, _ = run(box, ["project", "add", "analytics", REPO], stdin="yes\n")
    assert code == 0
    [(verb, plan)] = box.writes
    assert verb == "confirm"
    assert plan["name"] == "analytics"
    assert plan["revision"] == revisions(out)[-1]
    [repository] = plan["repositories"]
    assert repository["repo"] == REPO
    assert repository["setup_cmd"] == "pnpm install --frozen-lockfile"
    assert repository["verify_cmd"] == "pnpm e2e"
    assert repository["gate_cmd"] == "make gate"
    assert repository["compose_services"] == ["postgres", "redis"]
    assert plan["board"] == dict(owner="example", number=7)
    assert plan["models"] == "inherit"
    assert plan["secret_refs"] == ["op://Analytics/db/password"]
    assert REPO in str(plan["access"]) and REPO in str(plan["write_tests"])
    [action] = [w for w in plan["write_tests"] if REPO in w]
    assert action.strip() != REPO
    assert "temporary" in action.lower() and "branch" in action.lower()
    assert "pull request" in action.lower()
    [board_action] = [w for w in plan["write_tests"] if w.lower().startswith("board")]
    assert "restore" in board_action.lower()


def test_prompts_for_name_and_repository_when_not_supplied():
    box = Box()
    code, out, _ = run(box, ["project", "add"], stdin=f"analytics\n{REPO}\nyes\n")
    assert code == 0
    [(verb, plan)] = box.writes
    assert plan["name"] == "analytics"
    assert [r["repo"] for r in plan["repositories"]] == [REPO]


def test_a_project_may_name_several_repositories():
    box = Box()
    code, out, _ = run(box, ["project", "add", "analytics", REPO, "example/analytics-web"],
                       stdin="yes\n")
    assert code == 0
    [(verb, plan)] = box.writes
    assert [r["repo"] for r in plan["repositories"]] == [REPO, "example/analytics-web"]
    assert "example/analytics-web" in section(out, "Configuration:")
    assert "example/analytics-web" in section(out, "Write tests:")


def test_operator_edits_are_applied_under_a_new_revision():
    box = Box()
    code, out, _ = run(box, ["project", "add", "analytics", REPO],
                       stdin=f"edit {REPO}.verify_cmd=make e2e\nyes\n")
    assert code == 0
    first, last = revisions(out)[0], revisions(out)[-1]
    assert first != last
    [(verb, plan)] = box.writes
    assert plan["revision"] == last
    assert plan["repositories"][0]["verify_cmd"] == "make e2e"
    reprinted = out.split(f"Revision: {first}", 1)[1]
    assert "make e2e" in reprinted


def test_a_confirmation_does_not_cover_an_edit_made_after_it():
    box = Box()
    code, out, _ = run(box, ["project", "add", "analytics", REPO],
                       stdin=f"edit {REPO}.gate_cmd=make check\n")
    assert code == 0
    assert box.writes == []
    assert len(set(revisions(out))) == 2


def test_absent_setup_is_reported_for_correction_not_fabricated():
    bare = {REPO: dict(gate_cmd="make gate", board=dict(owner="example", number=7))}
    box = Box(inspected=bare)
    code, out, _ = run(box, ["project", "add", "analytics", REPO], stdin="no\n")
    assert code == 0
    missing = section(out, "Missing:")
    assert REPO in missing and "setup_cmd" in missing and "verify_cmd" in missing
    assert "gate_cmd" not in missing
    assert "Services:" in out
    for invented in ("npm", "pnpm", "make e2e", "pytest", "postgres"):
        assert invented not in out
    assert box.writes == []


def test_exiting_the_proposal_changes_nothing():
    for answer in ("no\n", "\n", ""):
        box = Box()
        code, out, _ = run(box, ["project", "add", "analytics", REPO], stdin=answer)
        assert code == 0
        assert "Write tests:" in out
        assert box.writes == []
        assert {r if isinstance(r, str) else r[0] for r in box.reads} <= {"projects", "inspect", "board_state"}


@pytest.mark.parametrize("spelling", [
    "https://github.com/Example/Analytics",
    "https://github.com/example/analytics.git",
    "git@github.com:example/analytics.git",
    "ssh://git@github.com/example/analytics",
])
def test_the_same_repository_cannot_become_a_second_project(spelling):
    box = Box(registered={"analytics": [REPO]})
    code, out, err = run(box, ["project", "add", "analytics-copy", spelling], stdin="yes\n")
    assert code != 0
    assert "analytics" in out + err
    assert "already" in (out + err).lower()
    assert box.writes == []


def test_a_duplicate_among_several_repositories_is_rejected_whole():
    box = Box(registered={"analytics": [REPO]})
    code, out, err = run(box, ["project", "add", "analytics-copy", "example/other",
                               "git@github.com:example/analytics.git"], stdin="yes\n")
    assert code != 0
    assert "analytics" in out + err
    assert box.writes == []


def test_repeating_onboarding_under_the_same_name_resumes_it():
    box = Box(registered={"analytics": [REPO]})
    code, out, err = run(box, ["project", "add", "analytics",
                               "https://github.com/example/analytics"], stdin="yes\n")
    assert code == 0
    assert box.writes == [("resume", "analytics")]
    assert "resum" in out.lower()


@pytest.mark.parametrize("stdin", ["no\n", ""], ids=["declined", "eof"])
def test_resuming_an_existing_project_needs_the_operators_confirmation(stdin):
    box = Box(registered={"analytics": [REPO]})
    code, out, err = run(box, ["project", "add", "analytics", REPO], stdin=stdin)
    assert code == 0
    assert box.writes == []


def test_changed_membership_of_an_existing_project_is_proposed_before_anything_runs():
    for answer, writes in (("no\n", 0), ("", 0), ("yes\n", 1)):
        box = Box(registered={"analytics": [REPO]})
        code, out, _ = run(box, ["project", "add", "analytics", REPO, "example/analytics-web"],
                           stdin=answer)
        assert code == 0
        assert "example/analytics-web" in section(out, "Configuration:")
        assert "example/analytics-web" in section(out, "Write tests:")
        assert len(box.writes) == writes
    [(verb, plan)] = box.writes
    assert verb == "confirm"
    assert plan["revision"] == revisions(out)[-1]
    assert [r["repo"] for r in plan["repositories"]] == [REPO, "example/analytics-web"]


def test_model_override_is_shown_and_applied_under_a_new_revision():
    box = Box()
    code, out, _ = run(box, ["project", "add", "analytics", REPO],
                       stdin="edit analytics.models=opus\nyes\n")
    assert code == 0
    first, last = revisions(out)[0], revisions(out)[-1]
    assert first != last
    assert "inherit" in out.split(f"Revision: {first}", 1)[0]
    reprinted = out.split(f"Revision: {first}", 1)[1]
    [models_line] = [l for l in reprinted.splitlines() if l.strip().startswith("models:")]
    assert "opus" in models_line and "inherit" not in models_line
    [(verb, plan)] = box.writes
    assert plan["models"] == "opus"
    assert plan["revision"] == last


def test_console_script_names_a_callable_in_the_project_cli_package():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    target = pyproject["project"]["scripts"]["agent-ops"]
    module, _, attr = target.partition(":")
    assert module.split(".")[0] == "project_cli"
    assert callable(getattr(import_module(module), attr))
    assert "project_cli" in pyproject["tool"]["setuptools"]["packages"]


# Board selection and creation edits (spec S02, S51)


class BoardBox:
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


def run_board(box, answers):
    import project_cli
    out, err = io.StringIO(), io.StringIO()
    code = project_cli.main(["project", "add", "analytics", REPO],
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
    box = BoardBox(initial)
    code, out, err = run_board(box, f"edit board={choice}\nyes\n")
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
    box = BoardBox()
    _, out, _ = run_board(box, "edit board=create:example/Analytics\n" + answer)
    assert "create example/Analytics" in out
    assert box.writes == []


def test_unresolved_board_cannot_be_confirmed():
    box = BoardBox()
    _, _, err = run_board(box, "yes\n")
    assert "board" in err.lower()
    assert box.writes == []


@pytest.mark.parametrize("choice", ["example/0", "example/nope", "create:/Title", "create:example/", ""])
def test_invalid_board_choice_preserves_displayed_plan(choice):
    board = {"owner": "example", "number": 7}
    box = BoardBox(board)
    _, _, err = run_board(box, f"edit board={choice}\nyes\n")
    assert "board" in err.lower()
    assert box.writes[0]["board"] == board
