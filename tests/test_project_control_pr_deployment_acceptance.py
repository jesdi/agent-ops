"""Acceptance tests for ticket 15 (project-onboarding): a confirmed durable
operation reaches the infrastructure PR, required checks, merge and verified
deployment, driven through the CONTROL seam.

Spec: specs/project-onboarding/spec.md S04, S22 (R2, R8, R12, R14).
Ticket: .agent/tickets/15-durable-control-pr-deployment.md.

Interface these tests define (the ticket leaves its shape open):
- `project_control.ProjectControl(state_dir, github, deployer)` runs on the
  box. Its operations live under `state_dir`, so a new instance on the same
  directory is the same service after a restart or after the operator's SSH
  connection dropped.
  - `propose(plan) -> dict`: a read; returns at least `revision`.
  - `confirm(plan) -> operation_id`: records the confirmed operation (the
    `dict` the `project_cli` hands to `box.confirm`). Confirming the same
    project and repositories again returns the same operation id.
  - `advance(operation_id) -> status`: one worker pass; it never waits for
    checks, approval or deployment, it records what it observed and returns.
    Unknown or unconfirmed ids raise `project_control.UnconfirmedOperation`.
  - `status(operation_id) -> dict` with `phase` ("published", "blocked",
    "merged", "complete"), `blocker` (text naming the cause, "" when none),
    `pr` ({"number", "url"} or None) and `applied_revision` (None until
    deployment of the merge revision is verified). A blocked operation stays
    resumable: a later `advance` continues it.
- `github` is the control credential's GitHub, a double here:
  `main() -> (sha, config)`, `find_pr(branch) -> pr | None`,
  `create_pr(branch, base_sha, config, title) -> pr`, `checks(number) ->
  {"state": "pending" | "passing" | "failing", "failing": [names]}`,
  `approval(number) -> {"required", "granted"}` and `merge(number, head_sha) ->
  merge_sha`. `config` is the whole versioned configuration the PR proposes
  (`{"projects": {name: {...}}}`), built from the current main.
- `deployer` is the updater on the box: `applied() -> {"revision"}` and
  `verify(revision) -> {"ok", "detail"}` (application/configuration
  compatibility).
"""
import threading
from pathlib import Path

import pytest

from dispatcher.convergence import pass_lock
from project_control import UnconfirmedOperation

REPO = "example/analytics"
TOKEN = "control-token-value-9f3a"
PLAN = dict(
    name="analytics", revision="r1",
    repositories=[dict(repo=REPO, setup_cmd="pnpm install", verify_cmd="pnpm e2e",
                       gate_cmd="make gate", compose_services=["postgres"])],
    board=dict(owner="example", number=7), models="inherit",
    access=[f"{REPO}: clone, push branches, open pull requests, board access"],
    secret_refs=[], write_tests=[f"{REPO}: temporary branch push, temporary pull request"],
)
BILLING = {"repositories": ["example/billing"], "desired_enabled": True, "models": "inherit"}


class Github:
    """The infrastructure repository as the control credential sees it."""

    token = TOKEN

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.sha, self.config = "s1", {"projects": {"billing": dict(BILLING)}}
        self.prs, self.created, self.merges, self.direct_writes = {}, [], [], []
        self.check_state = {"state": "passing", "failing": []}
        self.approval_state = {"required": False, "granted": True}
        self.lose_create_response = False
        self.lock_free = []

    def _probe(self):
        try:
            with pass_lock(str(self.state_dir), timeout=0.3):
                self.lock_free.append(True)
        except TimeoutError:
            self.lock_free.append(False)

    def main(self):
        self._probe()
        return self.sha, {"projects": {k: dict(v) for k, v in self.config["projects"].items()}}

    def advance_main(self, projects):
        self.config["projects"].update(projects)
        self.sha = f"s{int(self.sha[1:]) + 1}"

    def find_pr(self, branch):
        self._probe()
        return next((dict(p) for p in self.prs.values() if p["branch"] == branch), None)

    def create_pr(self, branch, base_sha, config, title):
        self._probe()
        number = 100 + len(self.created)
        self.created.append(branch)
        self.prs[number] = dict(number=number, url=f"https://github.com/infra/pull/{number}",
                                branch=branch, base_sha=base_sha, config=config,
                                head_sha=f"h{number}")
        if self.lose_create_response:
            self.lose_create_response = False
            raise ConnectionError("response lost after the PR was created")
        return dict(self.prs[number])

    def push_to_pr(self, number, extra_projects):
        self.prs[number]["config"]["projects"].update(extra_projects)
        self.prs[number]["head_sha"] += "x"

    def checks(self, number):
        self._probe()
        return dict(self.check_state)

    def approval(self, number):
        self._probe()
        return dict(self.approval_state)

    def merge(self, number, head_sha):
        self._probe()
        self.merges.append((number, head_sha))
        pr = self.prs[number]
        assert head_sha == pr["head_sha"], "merging a head the operation never confirmed"
        assert pr["base_sha"] == self.sha, "merging a PR built on a stale main"
        self.config = pr["config"]
        self.sha = f"m{number}"
        return self.sha

    def write_main(self, *a, **kw):
        self.direct_writes.append(("main", a, kw))


class Deployer:
    def __init__(self, state_dir, github):
        self.state_dir, self.github = state_dir, github
        self.auto = True
        self.compatible = {"ok": True, "detail": ""}
        self.live_config = {"projects": {"billing": dict(BILLING)}}
        self.direct_writes = []

    def applied(self):
        with pass_lock(str(self.state_dir), timeout=0.3):
            pass
        if self.auto and self.github.sha.startswith("m"):
            self.live_config = self.github.config
            return {"revision": self.github.sha}
        return {"revision": None}

    def verify(self, revision):
        return dict(self.compatible)

    def write_live_config(self, *a, **kw):
        self.direct_writes.append((a, kw))


BOARD_FIELDS = [dict(name="Status", type="single_select", options=["Inbox", "Ready", "In progress"]),
                dict(name="Impact", type="number"), dict(name="Effort", type="number"),
                dict(name="Score", type="number"), dict(name="Area", type="single_select", options=["general"])]
# The confirmed board as the trusted read verifies it; this is what publication records.
VERIFIED_BOARD = dict(PLAN["board"], id="PVT_7", fields={f["name"]: f"F_{f['name']}" for f in BOARD_FIELDS})


class Boards:
    """The confirmed board, already prepared: complete structure, no cards."""

    def board_state(self, ref):
        return dict(owner=ref["owner"], number=ref["number"], id=VERIFIED_BOARD["id"],
                    fields=[dict(f, id=VERIFIED_BOARD["fields"][f["name"]]) for f in BOARD_FIELDS], cards=[])


class Verifier:
    """Every required write is authorized, succeeds and is cleaned up."""

    def authorize(self, target):
        return dict(state="granted", action="")

    def write(self, target, kind):
        return f"{target}:{kind}"

    def cleanup(self, target, receipt):
        return dict(state="closed" if receipt["kind"] == "pr" else "removed", detail="")


@pytest.fixture
def world(tmp_path):
    from project_control import ProjectControl
    state = tmp_path / "state"
    github = Github(state)
    deployer = Deployer(state, github)

    def control():
        return ProjectControl(state_dir=str(state), github=github, deployer=deployer,
                              boards=Boards(), verifier=Verifier())
    return type("World", (), dict(state=state, github=github, deployer=deployer,
                                  control=staticmethod(control)))


def drive(control, op, passes=6):
    status = None
    for _ in range(passes):
        control.advance(op)
        status = control.status(op)
        if status["phase"] in ("complete", "blocked"):
            break
    return status


def test_a_failing_required_check_blocks_deployment_with_the_pr_named(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.check_state = {"state": "failing", "failing": ["unit-tests"]}
    status = drive(control, op)
    assert status["phase"] == "blocked"
    assert "unit-tests" in status["blocker"]
    assert status["pr"]["url"] in status["blocker"] or str(status["pr"]["number"]) in status["blocker"]
    assert world.github.merges == []
    assert world.github.direct_writes == [] and world.deployer.direct_writes == []
    assert "analytics" not in world.deployer.live_config["projects"]
    world.github.check_state = {"state": "passing", "failing": []}
    assert drive(control, op)["phase"] == "complete"
    assert len(world.github.created) == 1


def test_an_unmet_organization_approval_blocks_deployment_with_the_pr_named(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.approval_state = {"required": True, "granted": False}
    status = drive(control, op)
    assert status["phase"] == "blocked"
    assert "approval" in status["blocker"].lower()
    assert status["pr"]["url"] in status["blocker"] or str(status["pr"]["number"]) in status["blocker"]
    assert world.github.merges == []
    assert world.github.direct_writes == [] and world.deployer.direct_writes == []
    world.github.approval_state = {"required": True, "granted": True}
    assert drive(control, op)["phase"] == "complete"


def test_a_blocked_operation_keeps_the_current_effective_configuration(world):
    control = world.control()
    before = {"main": world.github.main()[1], "live": world.deployer.live_config}
    op = control.confirm(PLAN)
    world.github.check_state = {"state": "failing", "failing": ["lint"]}
    drive(control, op)
    assert world.github.main()[1] == before["main"]
    assert world.deployer.live_config == before["live"]
    assert world.github.direct_writes == [] and world.deployer.direct_writes == []
    assert control.status(op)["applied_revision"] is None


def test_rerunning_after_the_connection_is_lost_reuses_the_pr_and_completes_late(world):
    first = world.control()
    op = first.confirm(PLAN)
    world.github.check_state = {"state": "pending", "failing": []}
    first.advance(op)
    [pr] = [first.status(op)["pr"]]
    assert pr is not None and len(world.github.created) == 1
    del first  # the SSH session and its process are gone
    second = world.control()
    assert second.confirm(PLAN) == op
    world.deployer.auto = False
    second.advance(op)
    assert second.status(op)["phase"] != "complete"
    world.github.check_state = {"state": "passing", "failing": []}
    second.advance(op)
    assert second.status(op)["phase"] != "complete"  # merged is not applied or checked
    world.deployer.auto = True
    assert drive(second, op)["phase"] == "complete"
    assert len(world.github.created) == 1
    assert second.status(op)["pr"] == pr
    assert list(world.github.config["projects"]).count("analytics") == 1


def test_a_lost_create_response_is_reconciled_to_the_same_pr_before_another_create(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.lose_create_response = True
    world.github.check_state = {"state": "pending", "failing": []}
    try:
        control.advance(op)
    except ConnectionError:
        pass
    restarted = world.control()
    restarted.advance(op)
    restarted.advance(op)
    assert len(world.github.created) == 1
    assert restarted.status(op)["pr"]["number"] == 100
    world.github.check_state = {"state": "passing", "failing": []}
    assert drive(restarted, op)["phase"] == "complete"
    assert len(world.github.created) == 1


def test_confirmed_operation_state_survives_a_service_restart(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.check_state = {"state": "pending", "failing": []}
    control.advance(op)
    seen = control.status(op)
    reborn = world.control()
    assert reborn.status(op) == seen
    assert reborn.confirm(PLAN) == op


def test_a_stale_operation_keeps_unrelated_newer_configuration(world):
    control = world.control()
    op = control.confirm(PLAN)
    newer = dict(BILLING, models="anthropic/claude-opus-5-5")
    world.github.advance_main({"billing": newer, "payroll": dict(BILLING)})
    assert drive(control, op)["phase"] == "complete"
    projects = world.deployer.live_config["projects"]
    assert projects["billing"] == newer
    assert "payroll" in projects
    assert REPO in str(projects["analytics"])


def test_only_the_exact_confirmed_delta_reaches_the_merge(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.check_state = {"state": "pending", "failing": []}
    control.advance(op)
    number = control.status(op)["pr"]["number"]
    proposed = world.github.prs[number]["config"]["projects"]
    assert set(proposed) == {"billing", "analytics"}
    assert proposed["billing"] == BILLING
    assert proposed["analytics"]["models"] == PLAN["models"]
    assert proposed["analytics"]["board"] == VERIFIED_BOARD
    assert proposed["analytics"]["desired_enabled"] is False, "a new project starts paused (R12/R13)"
    assert [r if isinstance(r, str) else r["repo"] for r in proposed["analytics"]["repositories"]] == [REPO]
    published = proposed["analytics"]["repositories"][0]
    for key in ("setup_cmd", "verify_cmd", "gate_cmd", "compose_services"):
        assert published[key] == PLAN["repositories"][0][key], f"confirmed {key} must be published"
    world.github.push_to_pr(number, {"intruder": dict(BILLING)})
    world.github.check_state = {"state": "passing", "failing": []}
    status = drive(control, op)
    assert status["phase"] == "blocked"
    assert world.github.merges == []
    assert "intruder" not in world.github.config["projects"]


def test_a_github_merge_alone_is_not_an_applied_revision(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.deployer.auto = False
    status = drive(control, op)
    assert status["phase"] == "merged"
    assert status["applied_revision"] is None
    world.deployer.auto = True
    status = drive(control, op)
    assert status["phase"] == "complete"
    assert status["applied_revision"] == world.github.sha


def test_a_deployment_incompatible_with_the_application_is_not_complete(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.deployer.compatible = {"ok": False, "detail": "schema v3 needs application 2.1"}
    status = drive(control, op)
    assert status["phase"] == "blocked"
    assert "schema v3 needs application 2.1" in status["blocker"]
    assert status["applied_revision"] is None


def test_only_a_confirmed_operation_can_use_the_write_capability(world):
    control = world.control()
    control.propose(PLAN)
    with pytest.raises(UnconfirmedOperation):
        control.advance("op-never-confirmed")
    assert world.github.created == [] and world.github.merges == []
    assert world.github.direct_writes == []


def test_the_control_credential_never_reaches_stored_operation_state(world):
    control = world.control()
    op = control.confirm(PLAN)
    drive(control, op)
    assert TOKEN not in str(control.status(op))
    for path in Path(world.state).rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


def test_no_wait_holds_the_shared_admission_lock(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.check_state = {"state": "pending", "failing": []}
    control.advance(op)
    world.github.check_state = {"state": "passing", "failing": []}
    drive(control, op)
    assert world.github.lock_free and all(world.github.lock_free)
    held = threading.Event()
    done = threading.Event()

    def other_holder():
        with pass_lock(str(world.state)):
            held.set()
            done.wait(5)
    t = threading.Thread(target=other_holder)
    t.start()
    held.wait(5)
    try:
        # A claim in progress elsewhere does not stall operation reads.
        reader = threading.Thread(target=lambda: control.status(op))
        reader.start()
        reader.join(2)
        assert not reader.is_alive()
    finally:
        done.set()
        t.join()
