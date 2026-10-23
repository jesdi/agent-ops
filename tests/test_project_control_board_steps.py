"""Board work of a confirmed operation: create once, prepare, import, publish the verified board."""
import project_control
from project_control import ProjectControl

REPO = "example/analytics"
PREP = dict(fields=[dict(name="Impact", type="number")], options=[], labels={REPO: ["auto"]})


class Github:
    def __init__(self):
        self.created = []

    def main(self):
        return "s1", {"projects": {}}

    def find_pr(self, branch):
        return None

    def create_pr(self, branch, base_sha, config, title):
        self.created.append(config)
        return dict(number=1, url="u", head_sha="h", base_sha=base_sha, config=config)

    def checks(self, number):
        return dict(state="pending", failing=[])


class Boards:
    def __init__(self, lose_create=False):
        self.boards, self.imports, self.applied, self.lose_create = [], [], [], lose_create

    def find(self, owner, title):
        return [b for b in self.boards if b["title"] == title]

    def create(self, owner, title):
        ref = dict(owner=owner, number=7, id="PVT_7", title=title)
        self.boards.append(ref)
        if self.lose_create:
            self.lose_create = False
            raise TimeoutError("response lost")
        return ref

    def board_state(self, ref):
        from tests.test_project_board_onboarding_acceptance import FIELDS
        return {**ref, "id": ref.get("id", "PVT_7"), "fields": [{**f, "id": "F1" if f["name"] == "Impact" else f["name"]} for f in FIELDS], "cards": []}

    def apply(self, ref, prep):
        self.applied.append(prep)
        return dict(fields={"Impact": "F1"})

    def import_issues(self, ref, add):
        self.imports.append(add)


def plan(board, prep=PREP, add=None):
    return dict(name="analytics", revision="r1", repositories=[dict(repo=REPO)], board=board,
                models="inherit", access=[], secret_refs=[], write_tests=[],
                board_prep=prep, backlog=dict(add=add or {}, future="reconcile", status=None, labels=[]))


def control(tmp_path, boards):
    gh = Github()
    return ProjectControl(tmp_path, gh, None, boards), gh


def test_a_created_board_is_prepared_imported_and_published_with_its_verified_identity(tmp_path):
    boards = Boards()
    pc, gh = control(tmp_path, boards)
    op = pc.confirm(plan(dict(owner="example", create=True, title="T"), add={REPO: [2]}))
    pc.advance(op)
    pc.advance(op)
    assert boards.imports == [{REPO: [2]}] and len(boards.applied) == 1
    board = gh.created[0]["projects"]["analytics"]["board"]
    assert board["id"] == "PVT_7" and board["fields"]["Impact"] == "F1"


def test_a_lost_create_response_is_recovered_without_creating_a_second_board(tmp_path):
    boards = Boards(lose_create=True)
    pc, gh = control(tmp_path, boards)
    op = pc.confirm(plan(dict(owner="example", create=True, title="T")))
    pc.advance(op)
    assert pc.status(op)["phase"] == "blocked"
    pc.advance(op)
    assert len(boards.boards) == 1
    assert gh.created[0]["projects"]["analytics"]["board"]["number"] == 7


def test_board_work_without_an_adapter_blocks_instead_of_publishing(tmp_path):
    pc, gh = control(tmp_path, None)
    op = pc.confirm(plan(dict(owner="example", number=7)))
    pc.advance(op)
    assert pc.status(op)["phase"] == "blocked" and gh.created == []


def test_a_selected_board_with_nothing_to_add_still_requires_validation(tmp_path):
    pc, gh = control(tmp_path, None)
    op = pc.confirm(plan(dict(owner="example", number=7), prep=dict(fields=[], options=[], labels={})))
    pc.advance(op)
    assert not gh.created
    assert "no board adapter" in pc.status(op)["blocker"]
