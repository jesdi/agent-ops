"""Acceptance tests for ticket 18 (project-onboarding): provisioning the
confirmed checkouts and running resumable sandbox verification, driven through
the PROJECT_CLI seam (`agent-ops project status`), the box side behind it
(`project_control.ProjectControl`) and the dispatcher's shared execution slots.

Spec: specs/project-onboarding/spec.md S20, S22, S44, S45, S46 (R12, R20).
Ticket: .agent/tickets/18-provision-and-verify-project.md.

Interface these tests define on top of tests/test_project_authorization_write_verification_acceptance.py
(the ticket leaves its shape open):
- `ProjectControl(state_dir, github, deployer, boards=None, verifier=None, host=None)`.
  `host` is the box's host-side executor; without it no provisioning is
  required (earlier tickets' operations are unchanged). Its calls never raise
  for an expected failure; they return a result:
  - `ensure_clone(repo, path)`: idempotent; `ensure_dir(kind, path)` with kind
    "skills" | "storage" | "target": idempotent;
  - `has_secret(ref) -> bool`, `has_service(repo, service) -> bool`;
  - `start_services(repo, services, slot) -> {"ok", "detail", "receipts": [{"kind", "id"}]}`;
  - `run(repo, step, command, slot) -> {"ok", "detail"}`, step "setup" | "verify" | "gate";
  - `stop(receipt) -> {"state": "removed" | "failed", "detail"}`.
- The confirmed plan declares `locations = {"skills", "storage", "target", "app"}`
  and each repository its `clone_path`, `setup_cmd`, `verify_cmd`, `gate_cmd`,
  `compose_services`; `secret_refs` lists required secrets. `target` is where
  agent-ops works on the project; it must differ from, and not lie inside, `app`.
- `pc.provision(operation_id, slot=None) -> dict` runs (or resumes) the
  provisioning and verification of a confirmed operation in execution slot
  `slot`; `slot=None` means no free slot. Result and
  `pc.status(operation_id)["provisioning"]`:
  `{"state": "waiting_for_capacity" | "passed" | "failed", "blocker": str,
    "cleanup": [{"id", "state": "removed" | "failed", "detail"}]}`.
  `blocker` names the repository and the secret, service or step that stopped
  it. A confirmed operation that was never provisioned is "waiting_for_capacity".
  Provision state persists in `state_dir`, so a new instance resumes: finished
  clones and finished steps are not repeated, and leftover temporary resources
  of an interrupted run are stopped (never duplicated).
- `pc.verification()` is the `main.Deps.verification` of ticket 10: `pending()`
  lists operations not yet passed, `start(operation_id, slot)` provisions in
  that slot, `is_running(operation_id)` is False once it ended (slot released).
- `advance(operation_id)` does not merge the configuration PR while the
  provisioning has not passed (when a host is configured).
- `project status <name>`: `box.status(name)["provisioning"]` is the dict
  above; output section `Provisioning:` carries its state in words
  ("waiting for capacity", "passed", "failed") and its blocker; cleanup entries
  appear in `Cleanup:`. `Onboarding: complete` also needs provisioning `passed`
  whenever the status declares provisioning.
"""
import io
from dataclasses import replace

import pytest

import dispatcher.main as main
from dispatcher import state
from tests.test_main import FakeSessions, deps, patch_usage, patch_workspace
from tests.test_project_authorization_write_verification_acceptance import (
    BOARD_OK, Box, Forge, lines_about, repo_state, section, status as full_status, status_of)
from tests.test_project_reservations_acceptance import (
    Gh, live_slots, open_total, project, unfinished, world)

FRONT, BACK = "example/shop-frontend", "example/shop-backend"
REPO = "example/analytics"
LOC = dict(skills="/srv/agent-ops/skills", storage="/srv/agent-ops/state/analytics",
           target="/srv/agent-ops/analytics", app="/srv/agent-ops/app")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)


def member(repo, services=("postgres",)):
    name = repo.split("/")[1]
    return dict(repo=repo, clone_path=f"/srv/repos/{name}", setup_cmd=f"setup {name}",
                verify_cmd=f"verify {name}", gate_cmd=f"gate {name}",
                compose_services=list(services))


def plan(repos=(REPO,), name="analytics", secret_refs=(), locations=None):
    members = [member(r, ("postgres", "redis") if r == BACK else ("postgres",)) for r in repos]
    return dict(name=name, revision="r", models="inherit", board=None, repositories=members,
                secret_refs=list(secret_refs), locations=locations or dict(LOC))


class Host:
    def __init__(self):
        self.clone_calls, self.dir_calls, self.ran, self.started, self.stopped = [], [], [], [], []
        self.secrets, self.missing_services = set(), set()
        self.failing = {}          # (repo, step) -> detail
        self.interrupt = None      # (repo, step): raise ConnectionError once
        self.service_failure = None
        self.stop_failing = set()
        self.live, self.slots, self.counter = set(), [], 0

    def calls(self):
        return self.clone_calls + self.dir_calls + self.ran + self.started

    def ensure_clone(self, repo, path):
        self.clone_calls.append((repo, path))

    def ensure_dir(self, kind, path):
        self.dir_calls.append((kind, path))

    def has_secret(self, ref):
        return ref in self.secrets

    def has_service(self, repo, service):
        return (repo, service) not in self.missing_services

    def start_services(self, repo, services, slot):
        self.slots.append(slot)
        self.counter += 1
        receipts = [dict(kind="container", id=f"{repo}:{s}:{self.counter}") for s in services]
        self.live.update(r["id"] for r in receipts)
        self.started.append((repo, tuple(services)))
        if self.service_failure:
            return dict(ok=False, detail=self.service_failure, receipts=receipts)
        return dict(ok=True, detail="", receipts=receipts)

    def run(self, repo, step, command, slot):
        self.slots.append(slot)
        if self.interrupt == (repo, step):
            self.interrupt = None
            raise ConnectionError("connection lost")
        self.ran.append((repo, step, command))
        if (repo, step) in self.failing:
            return dict(ok=False, detail=self.failing[(repo, step)])
        return dict(ok=True, detail="")

    def stop(self, receipt):
        self.stopped.append(receipt["id"])
        if receipt["id"] in self.stop_failing:
            return dict(state="failed", detail="container did not stop")
        self.live.discard(receipt["id"])
        return dict(state="removed", detail="")


class Github:
    sha, merged = "s1", False

    def main(self):
        return self.sha, {"projects": {"analytics": {"repositories": [REPO]}} if self.merged else {}}

    def find_pr(self, branch):
        return None

    def create_pr(self, branch, base_sha, config, title):
        return dict(number=1, url="u", head_sha="h", base_sha=base_sha, config=config)

    def checks(self, number):
        return dict(state="passing", failing=[])

    def approval(self, number):
        return dict(required=False, granted=True)

    def merge(self, number, head_sha):
        self.merged, self.sha = True, "m1"
        return self.sha


class Deployer:
    def applied(self):
        return dict(revision="m1")

    def verify(self, revision):
        return dict(ok=True, detail="")


def control(tmp_path, host, **kw):
    from project_control import ProjectControl
    return ProjectControl(tmp_path / "control", kw.pop("github", Github()), kw.pop("deployer", None),
                          host=host, **kw)


def confirmed(tmp_path, host, **plan_kw):
    pc = control(tmp_path, host)
    return pc, pc.confirm(plan(**plan_kw))


def provisioning(pc, op):
    return pc.status(op)["provisioning"]


class ProvBox(Box):
    """`project status` renders what ProjectControl persisted."""

    def __init__(self, pc, op, repos):
        super().__init__(registered={"analytics": list(repos)})
        self.pc, self.op, self.repos = pc, op, repos

    def status(self, name):
        s = full_status({r: repo_state() for r in self.repos}, capabilities=BOARD_OK)
        s["provisioning"] = self.pc.status(self.op)["provisioning"]
        return s


# --- S44: a full box ---------------------------------------------------------

def _capacity_world(tmp_path, host):
    c = world(tmp_path, [project(tmp_path, "analytics")], capacity=2)
    live = unfinished(c, "analytics", 2, live=True)
    pc, op = confirmed(tmp_path, host)
    return c, live, pc, op


def _pass(c, pc, live):
    d = deps(Gh({}), FakeSessions(alive={t.issue for t in live}))
    d.verification = pc.verification()
    main.run_pass(c, d)


def test_s44_verification_waits_for_capacity_then_resumes_in_a_normal_free_slot(tmp_path):
    host = Host()
    c, live, pc, op = _capacity_world(tmp_path, host)

    _pass(c, pc, live)

    assert host.calls() == []                         # no extra sandbox, nothing cloned
    assert provisioning(pc, op)["state"] == "waiting_for_capacity"
    assert sorted(a.owner_kind for a in state.allocations(c.state_dir)) == ["task", "task"]
    code, out, err = status_of(ProvBox(pc, op, [REPO]))
    assert "waiting for capacity" in section(out, "Provisioning:")
    assert "Onboarding: incomplete" in out and "Onboarding: complete" not in out

    c = replace(c, capacity=3)                        # a normal slot became available
    _pass(c, pc, live)

    assert host.slots and set(host.slots) == {2}      # the free slot, beside slots 0 and 1
    assert provisioning(pc, op)["state"] == "passed"
    assert open_total(c) == 2                         # verification took no unfinished place
    code, out, err = status_of(ProvBox(pc, op, [REPO]))
    assert "passed" in section(out, "Provisioning:") and "Onboarding: complete" in out


# --- S46: a failed temporary setup -------------------------------------------

def test_s46_a_failed_temporary_database_setup_releases_the_slot_and_reports_cleanup(tmp_path):
    host = Host()
    host.service_failure = "temporary database setup failed"
    host.stop_failing = {f"{REPO}:postgres:1"}
    pc, op = confirmed(tmp_path, host)
    host.service_failure = "temporary database setup failed"
    pc2 = pc.verification()

    pc2.start(op, 0)

    result = provisioning(pc, op)
    assert result["state"] == "failed" and "temporary database setup failed" in result["blocker"]
    assert REPO in result["blocker"]
    assert pc2.is_running(op) is True                 # a container is still live: the slot stays held
    assert host.stopped                               # cleanup of the temporary sandbox was attempted
    failed = [e for e in result["cleanup"] if e["state"] == "failed"]
    assert [e["id"] for e in failed] == [f"{REPO}:postgres:1"]
    assert all(e["detail"] for e in failed)
    assert pc.status(op)["phase"] != "complete" and pc.status(op)["applied_revision"] is None
    code, out, err = status_of(ProvBox(pc, op, [REPO]))
    assert "failed" in section(out, "Provisioning:") and "Onboarding: incomplete" in out
    for e in failed:
        assert e["id"] in section(out, "Cleanup:") and e["detail"] in section(out, "Cleanup:")

    host.service_failure, host.stop_failing = None, set()
    assert pc.provision(op, 0)["state"] == "passed"   # resumable
    assert host.live == set()
    assert pc2.is_running(op) is False                # released once cleanup is confirmed


# --- locations ---------------------------------------------------------------

def test_clones_skills_ranking_and_task_storage_are_created_where_the_plan_declares_them(tmp_path):
    host = Host()
    pc, op = confirmed(tmp_path, host, repos=(FRONT, BACK), name="shop")
    assert pc.provision(op, 0)["state"] == "passed"
    assert sorted(host.clone_calls) == [(BACK, "/srv/repos/shop-backend"),
                                        (FRONT, "/srv/repos/shop-frontend")]
    assert dict(host.dir_calls) == {k: LOC[k] for k in ("skills", "storage", "target")}
    assert LOC["target"] != LOC["app"] and not LOC["target"].startswith(LOC["app"] + "/")


def test_a_target_that_is_the_running_application_is_refused_before_anything_is_created(tmp_path):
    host = Host()
    for target in (LOC["app"], LOC["app"] + "/projects/analytics"):
        pc, op = confirmed(tmp_path / target.replace("/", "_"), host,
                           locations=dict(LOC, target=target))
        result = pc.provision(op, 0)
        assert result["state"] == "failed" and "target" in result["blocker"]
        assert host.calls() == []


# --- what actually runs ------------------------------------------------------

def test_the_confirmed_commands_and_service_selections_are_what_runs_in_the_granted_slot(tmp_path):
    host = Host()
    pc, op = confirmed(tmp_path, host, repos=(FRONT, BACK), name="shop")
    assert pc.provision(op, 1)["state"] == "passed"
    assert sorted(host.ran) == sorted(
        (r, step, f"{step} {r.split('/')[1]}") for r in (FRONT, BACK)
        for step in ("setup", "verify", "gate"))
    assert sorted(host.started) == [(BACK, ("postgres", "redis")), (FRONT, ("postgres",))]
    assert set(host.slots) == {1}
    assert host.live == set()                         # temporary services stopped afterwards


@pytest.mark.parametrize("case", ["secret", "service", "check"])
def test_a_missing_definition_or_a_failed_check_names_its_blocker_and_leaves_the_project_paused(
        tmp_path, case):
    host = Host()
    kw = {}
    if case == "secret":
        kw["secret_refs"] = ["ANALYTICS_DB_URL"]
        needle = "ANALYTICS_DB_URL"
    elif case == "service":
        host.missing_services = {(REPO, "postgres")}
        needle = "postgres"
    else:
        host.failing = {(REPO, "verify"): "2 e2e tests failed"}
        needle = "verify"
    pc, op = confirmed(tmp_path, host, **kw)

    result = pc.provision(op, 0)

    assert result["state"] == "failed"
    assert REPO in result["blocker"] and needle in result["blocker"]
    if case != "check":
        assert host.ran == [] and host.started == []  # nothing ran on a missing definition
    assert host.live == set()
    assert pc.status(op)["phase"] != "complete" and pc.status(op)["applied_revision"] is None
    code, out, err = status_of(ProvBox(pc, op, [REPO]))
    assert needle in section(out, "Provisioning:")
    assert "Onboarding: incomplete" in out and "Onboarding: complete" not in out


# --- failure and interruption ------------------------------------------------

def test_a_rerun_after_a_failure_or_an_interruption_duplicates_no_clone_and_no_finished_step(tmp_path):
    host = Host()
    pc, op = confirmed(tmp_path, host, repos=(FRONT, BACK), name="shop")
    host.failing = {(BACK, "verify"): "backend e2e failed"}
    assert pc.provision(op, 0)["state"] == "failed"
    assert host.live == set()

    host.failing, host.interrupt = {}, (BACK, "gate")
    with pytest.raises(ConnectionError):
        pc.provision(op, 0)
    left = set(host.live)                             # the interrupted run left services behind

    again = control(tmp_path, host)                   # a restarted box service
    assert again.provision(op, 2)["state"] == "passed"

    assert host.live == set()
    steps = [s for s in host.ran]
    assert steps.count((FRONT, "setup", "setup shop-frontend")) == 1
    assert steps.count((FRONT, "verify", "verify shop-frontend")) == 1
    assert steps.count((FRONT, "gate", "gate shop-frontend")) == 1
    assert steps.count((BACK, "setup", "setup shop-backend")) == 1
    assert steps.count((BACK, "verify", "verify shop-backend")) == 2   # the failed one was rerun
    assert sorted(host.clone_calls) == [(BACK, "/srv/repos/shop-backend"),
                                        (FRONT, "/srv/repos/shop-frontend")]   # each cloned once


def test_only_confirmed_stopped_resources_are_reported_removed_and_failures_are_retried(tmp_path):
    host = Host()
    host.failing = {(REPO, "gate"): "gate failed"}
    host.stop_failing = {f"{REPO}:postgres:1"}
    pc, op = confirmed(tmp_path, host)

    result = pc.provision(op, 0)

    assert result["state"] == "failed"
    [entry] = [e for e in result["cleanup"] if e["id"] == f"{REPO}:postgres:1"]
    assert entry["state"] == "failed" and entry["detail"] == "container did not stop"
    assert f"{REPO}:postgres:1" in host.live         # still running, and not claimed as removed

    host.failing, host.stop_failing = {}, set()
    assert pc.provision(op, 0)["state"] == "passed"
    assert host.live == set()


# --- no accidental processing ------------------------------------------------

def test_the_configuration_pr_is_not_merged_while_verification_is_incomplete(tmp_path):
    from project_control import ProjectControl
    from tests.test_project_control_board_steps import Boards

    host, forge, github = Host(), Forge(), Github()
    host.failing = {(REPO, "verify"): "e2e failed"}
    pc = ProjectControl(tmp_path / "control", github, Deployer(), Boards(), verifier=forge, host=host)
    op = pc.confirm(dict(plan(), board=dict(owner="example", number=7), access=[], write_tests=[],
                         board_prep=dict(fields=[], options=[], labels={}),
                         backlog=dict(add={}, future="reconcile", status=None, labels=[])))
    assert pc.provision(op, 0)["state"] == "failed"
    for _ in range(5):
        pc.advance(op)
    assert github.merged is False                     # nothing claimable came from a merged PR
    assert pc.status(op)["phase"] != "complete" and pc.status(op)["applied_revision"] is None

    host.failing = {}
    assert pc.provision(op, 0)["state"] == "passed"
    for _ in range(5):
        pc.advance(op)
    assert github.merged is True and pc.status(op)["phase"] == "complete"


def test_a_status_declaring_unfinished_provisioning_is_never_reported_complete():
    for bad in (dict(state="waiting_for_capacity", blocker="", cleanup=[]),
                dict(state="failed", blocker=f"{REPO}: setup", cleanup=[])):
        s = full_status({REPO: repo_state()}, capabilities=BOARD_OK)
        s["provisioning"] = bad
        code, out, err = status_of(Box(s, registered={"analytics": [REPO]}))
        assert code == 0 and "Onboarding: incomplete" in out and "Onboarding: complete" not in out
