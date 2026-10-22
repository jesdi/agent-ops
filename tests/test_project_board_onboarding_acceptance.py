"""Acceptance tests for ticket 16 (project-onboarding): board selection,
non-destructive preparation and backlog import, driven through the
PROJECT_CLI seam (`agent-ops project add`).

Spec: specs/project-onboarding/spec.md S06-S11 (R4, R5, R6).
Ticket: .agent/tickets/16-board-onboarding-import.md.

Interface these tests define on top of tests/test_project_cli_proposal_acceptance.py
(the ticket leaves its shape open). The box stays read-only until `confirm`.
- `box.inspect(repo)` may also carry, all optional (absent = not declared, no
  validation or preparation is proposed for it):
  - `board_state`: the committed board as the box reads it:
    `{"fields": [{"name", "type", "options"?}], "cards": [{"repo", "number",
    "status"}]}`; `type` is "number", "text" or "single_select"; `options` are
    the option names of a single_select field.
  - `labels`: the repository's existing label names.
  - `open_issues`: `[{"number", "labels"}]`, the repository's open issues.
- `box.board_state(ref) -> board_state | None` reads the board an operator
  picked with `edit board=<owner>/<number>` (None = not found or no access).
- Required board structure: single_select `Status` with options `Inbox`,
  `Ready`, `In progress`; number `Impact`, `Effort`, `Score`; single_select
  `Area`. Required repository label: `auto`.
- Cards of repositories outside the project's membership (compared by
  `dispatcher.config.normalize_repo`) block the board; an incompatible or
  ambiguous field blocks it too. A blocked board prints the cause on stderr,
  names the repository or field, offers `edit board=` (choose or create
  another), and `yes` is refused (no `box.confirm`) until the board is changed.
- The plan handed to `box.confirm` gains
  `board_prep = {"fields": [{"name", "type", "options"?}],
  "options": [{"field", "name"}], "labels": {repo: [label]}}` (only what is
  missing; never a removal or replacement) and
  `backlog = {"add": {repo: [issue number]}, "future": "reconcile",
  "status": None, "labels": []}`: issues with no card (by repository-qualified
  identity) to add, the dispatcher's periodic reconcile as the future-issue
  mechanism, and nothing set on the added cards. The proposal prints sections
  `Board changes:` and `Backlog:`.
"""
import io
from copy import deepcopy

REPO = "example/analytics"
FRONT, BACK = "example/shop-frontend", "example/shop-backend"
REF = dict(owner="example", number=7)
LABEL = "auto"

FIELDS = [
    dict(name="Status", type="single_select", options=["Inbox", "Ready", "In progress", "Done"]),
    dict(name="Impact", type="number"), dict(name="Effort", type="number"),
    dict(name="Score", type="number"),
    dict(name="Area", type="single_select", options=["frontend", "backend"]),
]


def board(fields=FIELDS, cards=()):
    return dict(fields=deepcopy(fields), cards=[dict(c) for c in cards])


def card(repo, number, status=None):
    return dict(repo=repo, number=number, status=status)


class Box:
    """Read-only until `confirm`; any other call is a failure."""

    def __init__(self, inspected, others=None, registered=None):
        self.inspected, self.others = inspected, others or {}
        self.registered, self.writes, self.reads = registered or {}, [], []

    def projects(self):
        return self.registered

    def inspect(self, repo):
        self.reads.append(("inspect", repo))
        return deepcopy(self.inspected[repo])

    def board_state(self, ref):
        self.reads.append(("board_state", ref["owner"], ref["number"]))
        return deepcopy(self.others.get((ref["owner"], ref["number"])))

    def confirm(self, plan):
        self.writes.append(deepcopy(plan))
        return "op-1"

    def resume(self, name):
        self.writes.append(("resume", name))


def repo_info(state=None, ref=REF, labels=(LABEL,), issues=(), **extra):
    info = dict(setup_cmd="make setup", verify_cmd="make e2e", gate_cmd="make gate",
                labels=list(labels), open_issues=list(issues), **extra)
    if ref:
        info["board"] = dict(ref)
    if state is not None:
        info["board_state"] = state
    return info


def run(box, repos, stdin, name="analytics"):
    import project_cli
    out, err = io.StringIO(), io.StringIO()
    code = project_cli.main(["project", "add", name, *repos], stdin=io.StringIO(stdin),
                            stdout=out, stderr=err, box=box)
    return code, out.getvalue(), err.getvalue()


def section(text, heading):
    lines = text.splitlines()
    assert any(line.strip() == heading for line in lines), f"no {heading!r} section in:\n{text}"
    start = next(i for i, line in enumerate(lines) if line.strip() == heading) + 1
    body = []
    for line in lines[start:]:
        if not line.strip():
            break
        body.append(line)
    return "\n".join(body)


def issue(number, *labels):
    return dict(number=number, labels=list(labels))


def test_a_compatible_committed_board_is_reused_without_creating_another():  # S06
    state = board(cards=[card(REPO, 1, "Ready")])
    box = Box({REPO: repo_info(state)})
    code, out, err = run(box, [REPO], "yes\n")
    assert code == 0 and not err
    [plan] = box.writes
    assert plan["board"] == REF
    assert "create" not in plan["board"]
    prep = plan["board_prep"]
    assert prep["fields"] == [] and prep["options"] == []
    assert not any(prep["labels"].values())


def test_a_board_is_created_and_its_required_structure_proposed_for_a_repo_without_metadata():  # S07
    box = Box({REPO: repo_info(ref=None)})
    code, out, err = run(box, [REPO], "edit board=create:example/Analytics backlog\nyes\n")
    assert code == 0 and not err
    [plan] = box.writes
    assert plan["board"] == dict(owner="example", create=True, title="Analytics backlog")
    fields = {f["name"]: f for f in plan["board_prep"]["fields"]}
    assert set(fields) == {"Status", "Impact", "Effort", "Score", "Area"}
    assert {fields[n]["type"] for n in ("Impact", "Effort", "Score")} == {"number"}
    assert fields["Status"]["type"] == "single_select"
    assert {"Inbox", "Ready", "In progress"} <= set(fields["Status"]["options"])
    assert fields["Area"]["type"] == "single_select"
    changes = section(out, "Board changes:")
    for name in fields:
        assert name in changes


def test_a_card_of_an_unrelated_repository_rejects_the_board_and_waits_for_another_choice():  # S08
    foreign = board(cards=[card(REPO, 1), card("example/billing", 4, "Ready")])
    other = board(cards=[card(REPO, 2)])
    box = Box({REPO: repo_info(foreign)}, others={("example", 9): other})
    code, out, err = run(box, [REPO], "yes\nedit board=example/9\nyes\n")
    assert code == 0
    assert "example/billing" in err and "board" in err.lower()
    assert "edit board=" in err + out
    [plan] = box.writes  # only the confirmation of the replacement board
    assert plan["board"] == dict(owner="example", number=9)
    assert all(w[0] != "resume" for w in box.writes if isinstance(w, tuple))


def test_an_unrelated_board_alone_never_reaches_confirmation_or_any_write():  # S08
    foreign = board(cards=[card("example/billing", 4, "Ready")])
    box = Box({REPO: repo_info(foreign)})
    code, out, err = run(box, [REPO], "yes\nno\n")
    assert "example/billing" in err
    assert box.writes == []
    assert foreign["cards"] == [card("example/billing", 4, "Ready")]


def test_alternate_url_spelling_of_a_member_repository_is_not_an_outsider():  # R4
    state = board(cards=[card("https://github.com/example/analytics.git", 1)])
    box = Box({REPO: repo_info(state)})
    code, out, err = run(box, [REPO], "yes\n")
    assert code == 0 and len(box.writes) == 1
    assert box.writes[0]["board_prep"]["fields"] == []


def test_a_board_with_frontend_and_backend_cards_is_valid_for_both_members():  # R4
    state = board(cards=[card(FRONT, 12, "Ready"), card(BACK, 3)])
    box = Box({FRONT: repo_info(state), BACK: repo_info(state)})
    code, out, err = run(box, [FRONT, BACK], "yes\n", name="shop")
    assert code == 0 and not err
    [plan] = box.writes
    assert plan["board"] == REF
    assert plan["backlog"]["add"] == {} or not any(plan["backlog"]["add"].values())


def test_a_card_of_a_repository_outside_the_confirmed_members_blocks_selection():  # R4
    state = board(cards=[card(FRONT, 12), card(BACK, 3)])
    box = Box({FRONT: repo_info(state)})
    code, out, err = run(box, [FRONT], "yes\n", name="shop")
    assert BACK in err
    assert box.writes == []


def test_missing_structure_and_labels_are_proposed_then_added_without_touching_existing_data():  # S09
    existing = [f for f in FIELDS if f["name"] not in ("Impact",)]
    existing[0] = dict(existing[0], options=["Inbox", "Ready", "Done"])  # no "In progress"
    state = board(existing, cards=[card(REPO, 1, "Ready"), card(REPO, 2, "Done")])
    box = Box({REPO: repo_info(state, labels=["bug"], issues=[issue(1, "bug")])})
    code, out, err = run(box, [REPO], "yes\n")
    assert code == 0 and not err
    [plan] = box.writes
    prep = plan["board_prep"]
    assert prep["fields"] == [dict(name="Impact", type="number")]
    assert prep["options"] == [dict(field="Status", name="In progress")]
    assert prep["labels"] == {REPO: [LABEL]}
    assert plan["board"] == REF
    changes = section(out, "Board changes:")
    assert "Impact" in changes and "In progress" in changes and LABEL in changes
    assert "Effort" not in changes and "Area" not in changes
    assert "remove" not in changes.lower() and "replace" not in changes.lower()
    assert state["cards"][0] == card(REPO, 1, "Ready")


def test_proposed_additions_are_part_of_the_confirmed_revision_and_need_confirmation():  # S09
    state = board([f for f in FIELDS if f["name"] != "Effort"])
    box = Box({REPO: repo_info(state)})
    code, out, err = run(box, [REPO], "no\n")
    assert "Effort" in section(out, "Board changes:")
    assert box.writes == []
    box = Box({REPO: repo_info(state)})
    _, out, _ = run(box, [REPO], "yes\n")
    revisions = [l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("Revision:")]
    assert box.writes[0]["revision"] == revisions[-1]


def test_a_text_field_where_a_numeric_one_is_required_blocks_and_is_never_replaced():  # S10
    fields = [dict(f, type="text") if f["name"] == "Impact" else f for f in FIELDS]
    fixed = board()
    box = Box({REPO: repo_info(board(fields))}, others={("example", 8): fixed})
    code, out, err = run(box, [REPO], "yes\nedit board=example/8\nyes\n")
    assert "Impact" in err and "number" in err.lower() and "text" in err.lower()
    [plan] = box.writes
    assert plan["board"] == dict(owner="example", number=8)  # waited for the resolution
    assert all(f["name"] != "Impact" for f in plan["board_prep"]["fields"])


def test_the_incompatible_field_is_not_proposed_for_creation_or_replacement():  # S10
    fields = [dict(f, type="text") if f["name"] == "Impact" else f for f in FIELDS]
    box = Box({REPO: repo_info(board(fields))})
    code, out, err = run(box, [REPO], "yes\nno\n")
    assert box.writes == []
    assert "Impact" not in (section(out, "Board changes:") if "Board changes:" in out else "")


def test_two_fields_that_cannot_be_mapped_unambiguously_block_onboarding():  # S10
    fields = FIELDS + [dict(name="Impact", type="number")]
    box = Box({REPO: repo_info(board(fields))})
    code, out, err = run(box, [REPO], "yes\n")
    assert "Impact" in err and "ambiguous" in err.lower()
    assert box.writes == []


def test_an_unreadable_chosen_board_is_not_accepted():  # R4
    box = Box({REPO: repo_info(ref=None)})
    code, out, err = run(box, [REPO], "edit board=example/404\nyes\n")
    assert "board" in err.lower()
    assert box.writes == []


def test_existing_issues_are_added_once_and_keep_their_status_and_labels():  # S11
    state = board(cards=[card(REPO, 1, "Ready")])
    issues = [issue(1, LABEL), issue(2)]
    box = Box({REPO: repo_info(state, issues=issues)})
    code, out, err = run(box, [REPO], "yes\n")
    assert code == 0 and not err
    [plan] = box.writes
    backlog = plan["backlog"]
    assert backlog["add"] == {REPO: [2]}
    assert backlog["status"] is None and backlog["labels"] == []
    assert backlog["future"] == "reconcile"
    text = section(out, "Backlog:")
    assert REPO in text and "2" in text
    assert state["cards"] == [card(REPO, 1, "Ready")]


def test_repeating_the_import_adds_nothing_and_a_later_issue_is_added_once():  # S11
    after_first = board(cards=[card(REPO, 1, "Ready"), card(REPO, 2)])
    box = Box({REPO: repo_info(after_first, issues=[issue(1, LABEL), issue(2)])})
    run(box, [REPO], "yes\n")
    assert not any(box.writes[0]["backlog"]["add"].values())
    box = Box({REPO: repo_info(after_first, issues=[issue(1, LABEL), issue(2), issue(3)])})
    run(box, [REPO], "yes\n")
    assert box.writes[0]["backlog"]["add"] == {REPO: [3]}


def test_issue_identity_is_repository_qualified_across_member_repositories():  # R6
    state = board(cards=[card(FRONT, 1, "Ready")])
    info = {FRONT: repo_info(state, issues=[issue(1)]),
            BACK: repo_info(state, issues=[issue(1), issue(5)])}
    box = Box(info)
    code, out, err = run(box, [FRONT, BACK], "yes\n", name="shop")
    assert code == 0
    [plan] = box.writes
    assert plan["backlog"]["add"] == {BACK: [1, 5]} or plan["backlog"]["add"] == {FRONT: [], BACK: [1, 5]}


def test_the_periodic_reconcile_adds_each_missing_issue_once_and_leaves_cards_alone(tmp_path):  # S11
    """The configured dispatch cadence (the triage sweep) is the future-issue
    mechanism: driven against a board that remembers what was added."""
    import json
    from unittest.mock import patch
    from dispatcher import triage
    from dispatcher.config import Config, Project, ProjectRepository
    from tests.test_board import SCHEMA, _issue, _node
    from tests.test_triage_board_acceptance import Deps, Gh, OLD
    from tests.usagefakes import session_usage

    clone = tmp_path / "clone"
    (clone / ".backlog").mkdir(parents=True)
    (clone / ".backlog" / "project-meta.json").write_text(json.dumps(SCHEMA))

    class StatefulGh(Gh):
        def __call__(self, args, **kw):
            query = next((a for a in args if a.startswith("query=")), "")
            if "addProjectV2ItemById" in query:
                node = next(a for a in args if a.startswith("c=")).split("=", 1)[1]
                self.mutations.append(node)
                if not any(n["id"] == f"card-{node}" for n in self.nodes):
                    self.nodes.append(_node(f"card-{node}", int(node.removeprefix("ISSUE"))))
                return subprocess.CompletedProcess(args, 0, "{}", "")
            if args[1:4] == ["issue", "view", args[3]] and "id" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({"id": f"ISSUE{args[3]}"}), "")
            return super().__call__(args, **kw)

    import subprocess
    gh = StatefulGh([_node("I1", 1, status="Ready", impact=5, effort=1, area="infra")],
                    [_issue(1), _issue(2)])
    member = ProjectRepository(repo="o/a", clone_path=str(clone), worktrees_path=str(tmp_path / "wt"),
                               rank_cmd="rank", gate_cmd="make gate")
    project = Project(key="p", name="p", board_owner="o", board_number=1, status_field_id="F_STATUS",
                      status_ready_option_id="o_ready", status_in_progress_option_id="o_prog",
                      repositories=[member])
    cfg = Config(state_dir=str(tmp_path / "state"), capacity=2, session_memory="1500m",
                 session_cpus="2", targets=[], projects=[project])

    def sweep():
        triage.save_cursors(cfg.state_dir, {"o/a": OLD})
        with patch.object(triage, "fetch_all", return_value={"anthropic": session_usage(0.1)}), \
             patch.object(triage, "_run_session", return_value={"issues": []}):
            triage.run_sweep(cfg, Deps(), run=gh)

    sweep()
    sweep()  # a repeat adds nothing
    assert gh.mutations == ["ISSUE2"]
    gh.issues.append(_issue(3))  # a later issue
    sweep()
    sweep()
    assert gh.mutations == ["ISSUE2", "ISSUE3"]
    cards = {n["content"]["number"]: n for n in gh.nodes}
    assert cards[1]["status"] == {"name": "Ready"} and cards[1]["impact"] == {"number": 5}
    assert cards[2]["status"] is None and cards[3]["status"] is None


class Boards:
    """A board adapter that remembers: one card per repository-qualified issue."""

    def __init__(self, cards, fail_import=0):
        self.cards, self.fail_import = list(cards), fail_import

    def find(self, owner, title):
        return []

    def create(self, owner, title):
        return dict(owner=owner, number=7, id="PVT_7")

    def board_state(self, ref):
        return {**ref, "id": "PVT_7", "fields": [{**f, "id": f"F_{f['name']}"} for f in FIELDS],
                "cards": self.cards}

    def apply(self, ref, prep):
        return {"fields": {}}

    def import_issues(self, ref, add):
        for repo, numbers in add.items():
            for n in numbers:
                if (repo, n) not in [(c["repo"], c["number"]) for c in self.cards]:
                    self.cards.append(card(repo, n))
        if self.fail_import:
            self.fail_import -= 1
            raise RuntimeError("lost response")


def test_the_confirmed_import_adds_each_issue_once_across_retries_and_keeps_existing_cards(tmp_path):  # S11
    from tests.test_project_control_pr_deployment_acceptance import (
        Deployer, Github, PLAN, Verifier, drive)
    from project_control import ProjectControl

    existing = [card(REPO, 1, "Ready")]
    box = Box({REPO: repo_info(board(cards=existing), issues=[issue(1, LABEL), issue(2), issue(3)])})
    assert run(box, [REPO], "yes\n")[0] == 0
    [plan] = box.writes
    assert plan["backlog"]["add"] == {REPO: [2, 3]}

    state = tmp_path / "state"
    github = Github(state)
    boards = Boards(existing, fail_import=1)
    control = ProjectControl(state_dir=str(state), github=github,
                             deployer=Deployer(state, github), boards=boards, verifier=Verifier())
    op = control.confirm({**PLAN, "board": plan["board"], "board_prep": plan["board_prep"],
                          "backlog": plan["backlog"]})
    drive(control, op)
    drive(control, op)  # the retry after the lost response
    drive(control, op)
    assert [(c["repo"], c["number"]) for c in boards.cards] == [(REPO, 1), (REPO, 2), (REPO, 3)]
    assert boards.cards[0] == card(REPO, 1, "Ready")
    assert all(c["status"] is None for c in boards.cards[1:])


class WorldBoards:
    """A board adapter over a remembered world: boards, fields, options, labels, cards."""

    def __init__(self, boards=(), lose_create=False):
        self.boards = [dict(b) for b in boards]
        self.lose_create, self.labels = lose_create, {}

    def find(self, owner, title):
        return [dict(owner=b["owner"], number=b["number"], id=b["id"])
                for b in self.boards if b["owner"] == owner and b["title"] == title]

    def create(self, owner, title):
        board = dict(owner=owner, title=title, number=7 + len(self.boards), id=f"PVT_{7 + len(self.boards)}",
                     fields=[], cards=[])
        self.boards.append(board)
        if self.lose_create:
            self.lose_create = False
            raise ConnectionError("response lost after the board was created")
        return dict(owner=owner, number=board["number"], id=board["id"])

    def _board(self, ref):
        return next(b for b in self.boards if b["number"] == ref["number"])

    def board_state(self, ref):
        live = self._board(ref)
        return {**live, "fields": [{**f, "id": f"F_{f['name']}"} for f in live["fields"]], "cards": live["cards"]}

    def apply(self, ref, prep):
        board = self._board(ref)
        fields = {f["name"]: f for f in board["fields"]}
        for f in prep["fields"]:
            fields.setdefault(f["name"], deepcopy(f))
        for o in prep["options"]:
            if o["name"] not in fields[o["field"]]["options"]:
                fields[o["field"]]["options"].append(o["name"])
        board["fields"] = list(fields.values())
        for repo, names in prep["labels"].items():
            self.labels.setdefault(repo, set()).update(names)
        return {"fields": {name: f"F_{name}" for name in fields}}

    def import_issues(self, ref, add):
        return Boards(self._board(ref)["cards"]).import_issues(ref, add)


def _control(tmp_path, boards):
    from tests.test_project_control_pr_deployment_acceptance import Deployer, Github, Verifier
    from project_control import ProjectControl
    state = tmp_path / "state"
    github = Github(state)
    return ProjectControl(state_dir=str(state), github=github, deployer=Deployer(state, github),
                          boards=boards, verifier=Verifier()), github


def _confirmed(box, stdin):
    from tests.test_project_control_pr_deployment_acceptance import PLAN
    assert run(box, [REPO], stdin)[0] == 0
    [plan] = box.writes
    return {**PLAN, "board": plan["board"], "board_prep": plan["board_prep"], "backlog": plan["backlog"]}


def test_the_confirmed_creation_makes_one_verified_board_with_its_structure_across_a_lost_response(tmp_path):  # S07
    from tests.test_project_control_pr_deployment_acceptance import drive
    plan = _confirmed(Box({REPO: repo_info(ref=None)}), "edit board=create:example/Analytics backlog\nyes\n")
    world = WorldBoards(lose_create=True)
    control, github = _control(tmp_path, world)
    op = control.confirm(plan)
    assert drive(control, op)["phase"] == "blocked"  # the response was lost
    assert drive(control, op)["phase"] == "complete"
    [created] = world.boards  # recovered by title, never a second board
    assert {f["name"] for f in created["fields"]} == {"Status", "Impact", "Effort", "Score", "Area"}
    published = github.config["projects"]["analytics"]["board"]
    assert published["number"] == created["number"] and published["id"] == created["id"]


def test_the_confirmed_preparation_adds_only_what_is_missing_and_keeps_existing_data(tmp_path):  # S09
    from tests.test_project_control_pr_deployment_acceptance import drive
    existing = [f for f in FIELDS if f["name"] != "Impact"]
    existing[0] = dict(existing[0], options=["Inbox", "Ready", "Done"])
    cards = [card(REPO, 1, "Ready"), card(REPO, 2, "Done")]
    state = board(existing, cards=cards)
    plan = _confirmed(Box({REPO: repo_info(state, labels=["bug"], issues=[issue(1, "bug")])}), "yes\n")
    world = WorldBoards([dict(owner="example", title="t", number=7, id="PVT_7",
                              fields=deepcopy(existing), cards=deepcopy(cards))])
    control, _ = _control(tmp_path, world)
    assert drive(control, control.confirm(plan))["phase"] == "complete"
    [live] = world.boards
    fields = {f["name"]: f for f in live["fields"]}
    assert fields["Impact"]["type"] == "number"
    assert fields["Status"]["options"] == ["Inbox", "Ready", "Done", "In progress"]
    assert fields["Area"] == FIELDS[4] and fields["Score"] == FIELDS[3]
    assert world.labels == {REPO: {LABEL}}
    assert live["cards"] == cards
