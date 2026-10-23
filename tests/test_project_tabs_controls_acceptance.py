"""Acceptance tests for ticket 19 (project-onboarding): project tabs and the
PR-backed enable / pause controls, driven through the PROJECT_API seam
(`web.app.create_app` over `ProjectControl`) and the `agent-ops project` CLI.

Spec: specs/project-onboarding/spec.md S23, S25, S26, S52 (R3, R13, R14).
Ticket: .agent/tickets/19-project-tabs-and-controls.md.

Interface these tests define (the ticket leaves its shape open):
- `create_app(boot, sources, ..., project_control=None, config_path=None)`.
  `config_path` is the live targets.yaml; when given, every request reads the
  project configuration from it again, so a project deployed after the web
  started appears without a restart. Without it `boot` is used.
- `GET /api/projects` -> `{"projects": [tab, ...]}`, one tab per configured
  project (never per repository), in file order, each
  `{"name", "state": "enabled" | "paused", "repositories": ["owner/name", ...],
    "pending": None | {"operation", "requested": "enabled" | "paused",
                       "phase": "published" | "blocked" | "merged",
                       "blocker": str, "pr": {"number", "url"} | None}}`.
  `state` is the effective state, read from the deployed configuration;
  `pending` is the not yet deployed change request. A change that finished
  deployment has `pending` None.
- `POST /api/projects/{name}/enable` and `/pause` with `{"confirm": true}`
  -> 202 `{"operation": id}`; without the confirmation 4xx and nothing is
  created; an unknown project is 404.
- `ProjectControl.request_enabled(name, enabled) -> operation_id` is the one
  confirmed control operation behind the web and the terminal. It runs the
  ordinary PR / checks / approval / merge / verified-deployment sequence
  through `advance`; asking again for the same pending change returns the same
  operation. It changes only `desired_enabled` of that project's entry in the
  versioned configuration (the key ticket 15's delta writes). Unknown project:
  `project_control.UnknownProject`.
  `ProjectControl.pending_change(name) -> dict | None` is the `pending` above.
- Terminal: `agent-ops project enable|pause <name>` prints what will happen and
  asks; `yes` on stdin confirms and calls `box.request_enabled(name, enabled)`
  (the only write); anything else changes nothing. `agent-ops project state
  <name>` reads `box.control_state(name) -> {"effective": "enabled" | "paused",
  "pending": <as above> | None}` and prints `Effective: <state>` and, while a
  change is pending, `Pending: <requested> (<phase>)` plus the blocker text.
"""
import io
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import dispatcher.main as main
from dispatcher.config import load_config
from project_control import ProjectControl
from tests.test_main import FakeSessions, deps, patch_usage, patch_workspace
from tests.test_project_authorization_write_verification_acceptance import Forge
from tests.test_project_control_pr_deployment_acceptance import Github
from tests.test_project_provisioning_verification_acceptance import Host
from tests.test_project_reservations_acceptance import Gh, issues, run
from tests.webfakes import HEADERS, FakeSources
from web.app import create_app

ANALYTICS = "example/analytics"
FRONT, BACK = "example/shop-frontend", "example/shop-backend"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)


def entry(tmp_path, repos, enabled, board=7):
    return dict(repositories=[dict(repo=r, clone_path=str(tmp_path / "clones" / r.split("/")[1]))
                              for r in repos],
                board=dict(owner="example", number=board), desired_enabled=enabled, models="inherit")


def write_live(path, state_dir, config):
    """The updater on the box: versioned configuration -> the live targets.yaml."""
    projects = []
    for name, p in config["projects"].items():
        projects.append(dict(
            key=name, name=name, board_owner=p["board"]["owner"], board_number=p["board"]["number"],
            status_field_id="F", status_ready_option_id="R", status_in_progress_option_id="I",
            enabled=p["desired_enabled"],
            repositories=[dict(repo=r["repo"], clone_path=r["clone_path"],
                               worktrees_path=r["clone_path"] + ".wt", rank_cmd="rank",
                               gate_cmd="make gate") for r in p["repositories"]]))
    path.write_text(yaml.safe_dump(dict(state_dir=str(state_dir), capacity=3, targets=[],
                                        projects=projects)))


class Deployer:
    """`roll_out` is the updater applying the merged revision; an incompatible
    configuration is rejected and the live file stays as it was."""

    def __init__(self, github, path, state_dir):
        self.github, self.path, self.state_dir = github, path, state_dir
        self.revision, self.compatible = github.sha, dict(ok=True, detail="")
        self.direct_writes = []

    def roll_out(self):
        self.revision = self.github.sha
        if self.compatible["ok"]:
            write_live(self.path, self.state_dir, self.github.config)

    def applied(self):
        return dict(revision=self.revision)

    def verify(self, revision):
        return dict(self.compatible)

    def write_live_config(self, *a, **kw):
        self.direct_writes.append((a, kw))


class Boards:
    """A shared board holding issues of both member repositories."""

    def __init__(self, repos):
        self.repos, self.imported = repos, []
        self.cards = [dict(repo=r, number=1, status="Ready") for r in repos]

    def board_state(self, ref):
        fields = [dict(id="f1", name="Status", type="single_select", options=["Inbox", "Ready", "In progress"]),
                  dict(id="f2", name="Impact", type="number"), dict(id="f3", name="Effort", type="number"),
                  dict(id="f4", name="Score", type="number"),
                  dict(id="f5", name="Area", type="single_select", options=["frontend", "backend"])]
        return dict(owner="example", number=8, id="PVT_8", fields=fields,
                    cards=list(self.cards))

    def find(self, owner, title):
        return []

    def apply(self, ref, prep):
        return {"fields": {}}

    def import_issues(self, ref, add):
        for repo, numbers in add.items():
            self.imported.append((repo, sorted(numbers)))
            self.cards += [dict(repo=repo, number=n, status=None) for n in numbers]


class Rig:
    def __init__(self, tmp_path, *, analytics_enabled=True, with_shop=False, host=None):
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.path = tmp_path / "targets.yaml"
        self.gh = Github(self.state)
        self.gh.config = {"projects": {"analytics": entry(tmp_path, [ANALYTICS], analytics_enabled)}}
        if with_shop:
            self.gh.config["projects"]["shop"] = entry(tmp_path, [FRONT, BACK], False, board=8)
        self.gh.sha = "s1"
        self.dep = Deployer(self.gh, self.path, self.state)
        write_live(self.path, self.state, self.gh.config)
        self.boards = Boards([FRONT, BACK])
        self.pc = ProjectControl(str(tmp_path / "control"), self.gh, self.dep,
                                 boards=self.boards, verifier=Forge(), host=host)
        self.client = TestClient(create_app(load_config(self.path), FakeSources(str(self.state)),
                                            project_control=self.pc, config_path=str(self.path)))

    def tabs(self):
        r = self.client.get("/api/projects", headers=HEADERS)
        assert r.status_code == 200, r.text
        return {t["name"]: t for t in r.json()["projects"]}

    def request(self, name, verb, **kw):
        return self.client.post(f"/api/projects/{name}/{verb}", headers=HEADERS,
                                json={"confirm": True}, **kw)

    def advance(self, op, passes=4):
        for _ in range(passes):
            self.pc.advance(op)
            if self.pc.status(op)["phase"] in ("complete", "blocked"):
                break

    def config(self):
        return load_config(self.path)

    def terminal(self):
        return TermBox(self)

    def claims(self, candidates, sess):
        gh = Gh({"analytics": candidates})
        run(self.config(), gh, sess)
        return gh.claimed_by


class TermBox:
    """The box over SSH, backed by the same control service and live file."""

    def __init__(self, rig):
        self.rig, self.writes = rig, []

    def projects(self):
        return {p.key: [r.repo for r in p.repositories] for p in self.rig.config().projects}

    def request_enabled(self, name, enabled):
        self.writes.append(("request_enabled", name, enabled))
        return self.rig.pc.request_enabled(name, enabled)

    def control_state(self, name):
        project = next(p for p in self.rig.config().projects if p.key == name)
        return dict(effective="enabled" if project.enabled else "paused",
                    pending=self.rig.pc.pending_change(name))


def cli(box, *argv, stdin=""):
    import project_cli
    out, err = io.StringIO(), io.StringIO()
    code = project_cli.main(list(argv), stdin=io.StringIO(stdin), stdout=out, stderr=err, box=box)
    return code, out.getvalue(), err.getvalue()


def state_lines(rig, name="analytics"):
    code, out, err = cli(rig.terminal(), "project", "state", name)
    assert code == 0, err
    return out


# --- S23 / S52: tabs ---------------------------------------------------------

def test_s23_paused_project_has_its_own_tab_with_no_tasks_and_the_dispatcher_claims_nothing(tmp_path):
    rig = Rig(tmp_path, analytics_enabled=False, with_shop=True)
    tabs = rig.tabs()
    assert list(tabs) == ["analytics", "shop"]  # empty projects are visible; no tab per repository
    assert tabs["analytics"]["state"] == "paused" and tabs["analytics"]["pending"] is None
    assert tabs["analytics"]["repositories"] == [ANALYTICS]
    assert set(tabs["shop"]["repositories"]) == {FRONT, BACK} and tabs["shop"]["state"] == "paused"
    sess = FakeSessions()
    assert rig.claims(issues(100, 1), sess) == []


def test_s52_onboarded_shop_appears_as_one_paused_tab_after_deployment_without_a_restart(tmp_path):
    host = Host()
    rig = Rig(tmp_path, host=host)
    assert list(rig.tabs()) == ["analytics"]
    plan = dict(name="shop", revision="r1", models="inherit", secret_refs=[],
                board=dict(owner="example", number=8, id="PVT_8"),
                backlog=dict(add={FRONT: [2], BACK: [3]}),
                repositories=[dict(repo=r, clone_path=str(tmp_path / "clones" / r.split("/")[1]),
                                   setup_cmd="s", verify_cmd="v", gate_cmd="g", compose_services=[])
                              for r in (FRONT, BACK)])
    op = rig.pc.confirm(plan)
    assert rig.pc.provision(op, slot=1)["state"] == "passed"
    rig.advance(op)
    assert rig.pc.status(op)["phase"] == "merged"
    clones = {r: str(tmp_path / "clones" / r.split("/")[1]) for r in (FRONT, BACK)}
    assert sorted(host.clone_calls) == sorted(clones.items())  # both checkouts
    assert {(r, step) for r, step, _ in host.ran} == {(r, s) for r in (FRONT, BACK)
                                                      for s in ("setup", "verify", "gate")}
    assert sorted(rig.boards.imported) == [(BACK, [3]), (FRONT, [2])]  # open issues of both feed the board
    assert {(c["repo"], c["number"]) for c in rig.boards.cards} >= {(FRONT, 2), (BACK, 3)}
    assert list(rig.tabs()) == ["analytics"]  # merged, not deployed: not there yet
    rig.dep.roll_out()
    rig.advance(op)
    assert rig.pc.status(op)["phase"] == "complete"
    tabs = rig.tabs()
    assert list(tabs) == ["analytics", "shop"]
    assert tabs["shop"]["state"] == "paused" and tabs["shop"]["pending"] is None
    assert sorted(tabs["shop"]["repositories"]) == [BACK, FRONT]
    assert FRONT not in tabs and BACK not in tabs


# --- S25 ---------------------------------------------------------------------

def test_s25_pause_shows_enabled_as_effective_and_pause_as_pending_until_it_is_deployed(tmp_path):
    rig = Rig(tmp_path)
    sess = FakeSessions()
    assert rig.claims(issues(100, 1), sess) == [("analytics", 100)]
    before = rig.path.read_text()
    rig.gh.check_state = {"state": "pending", "failing": []}
    r = rig.request("analytics", "pause")
    assert r.status_code == 202, r.text
    op = r.json()["operation"]
    rig.advance(op)
    for tab in (rig.tabs()["analytics"],):
        assert tab["state"] == "enabled"
        assert tab["pending"]["requested"] == "paused" and tab["pending"]["phase"] == "published"
        assert tab["pending"]["operation"] == op and tab["pending"]["pr"]["number"] >= 100
    out = state_lines(rig)
    assert "Effective: enabled" in out and "Pending: paused (published)" in out
    rig.gh.check_state = {"state": "passing", "failing": []}
    rig.advance(op)
    assert rig.tabs()["analytics"]["pending"]["phase"] == "merged"  # waiting for the deployment
    assert rig.tabs()["analytics"]["state"] == "enabled"
    assert "Effective: enabled" in state_lines(rig) and "Pending: paused (merged)" in state_lines(rig)
    assert rig.path.read_text() == before and rig.dep.direct_writes == [] and rig.gh.direct_writes == []
    rig.dep.roll_out()
    rig.advance(op)
    tab = rig.tabs()["analytics"]
    assert tab["state"] == "paused" and tab["pending"] is None
    out = state_lines(rig)
    assert "Effective: paused" in out and "Pending" not in out
    assert rig.claims(issues(101, 1), sess) == []  # new claims stopped


# --- S26 ---------------------------------------------------------------------

def _failing_checks(rig):
    rig.gh.check_state = {"state": "failing", "failing": ["e2e-suite"]}
    return "e2e-suite"


def _approval_wait(rig):
    rig.gh.approval_state = {"required": True, "granted": False}
    return "approval"


def _failed_deployment(rig):
    rig.dep.compatible = dict(ok=False, detail="schema mismatch on boot")
    return "schema mismatch on boot"


@pytest.mark.parametrize("fail", [_failing_checks, _approval_wait, _failed_deployment])
def test_s26_a_failed_enable_leaves_the_project_paused_and_visible_as_blocked(tmp_path, fail):
    rig = Rig(tmp_path, analytics_enabled=False)
    why = fail(rig)
    box = rig.terminal()
    code, out, err = cli(box, "project", "enable", "analytics", stdin="yes\n")
    assert code == 0, err
    [(_, name, enabled)] = box.writes
    assert (name, enabled) == ("analytics", True)
    [op] = rig.pc.operations()
    rig.advance(op)
    if fail is _failed_deployment:
        rig.dep.roll_out()
        rig.advance(op)
    tab = rig.tabs()["analytics"]
    assert tab["state"] == "paused"
    assert tab["pending"]["requested"] == "enabled" and tab["pending"]["phase"] == "blocked"
    assert why in tab["pending"]["blocker"]
    out = state_lines(rig)
    assert "Effective: paused" in out and "Pending: enabled (blocked)" in out and why in out
    assert rig.claims(issues(100, 1), FakeSessions()) == []  # no override makes the dispatcher claim


# --- one operation, no override ---------------------------------------------

def test_web_and_terminal_share_one_operation_that_changes_only_that_projects_enabled_flag(tmp_path):
    rig = Rig(tmp_path, with_shop=True)
    before = {k: dict(v) for k, v in rig.gh.config["projects"].items()}
    op = rig.request("analytics", "pause").json()["operation"]
    box = rig.terminal()
    assert cli(box, "project", "pause", "analytics", stdin="yes\n")[0] == 0
    assert box.writes == [("request_enabled", "analytics", False)]
    assert rig.pc.operations() == [op]
    rig.advance(op)
    assert len(rig.gh.created) == 1 and rig.gh.direct_writes == []
    [pr] = rig.gh.prs.values()
    after = pr["config"]["projects"]
    assert after["shop"] == before["shop"]
    assert after["analytics"] == {**before["analytics"], "desired_enabled": False}


def test_the_terminal_changes_nothing_without_a_confirmation_or_for_an_unknown_project(tmp_path):
    rig = Rig(tmp_path)
    box = rig.terminal()
    assert cli(box, "project", "pause", "analytics", stdin="no\n")[0] == 0
    assert cli(box, "project", "pause", "analytics", stdin="")[0] == 0
    assert cli(box, "project", "pause", "ghost", stdin="yes\n")[0] != 0
    assert box.writes == [] and rig.pc.operations() == []
    assert cli(box, "project", "pause", "analytics", stdin="yes\n")[0] == 0  # the same command, confirmed
    assert len(rig.pc.operations()) == 1


# --- authenticated, same-origin, confirmed -----------------------------------

def test_only_a_confirmed_same_origin_operator_request_creates_a_control_operation(tmp_path):
    rig = Rig(tmp_path, with_shop=True)
    url = "/api/projects/analytics/pause"
    task_session = {"Authorization": "Bearer ghs_task_repository_token"}
    assert rig.client.post(url, json={"confirm": True}, headers=task_session).status_code == 401
    assert rig.client.post(url, json={"confirm": True},
                           headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
    unconfirmed = rig.client.post(url, headers=HEADERS, json={})
    assert 400 <= unconfirmed.status_code < 500 and unconfirmed.status_code != 404
    assert rig.pc.operations() == [] and rig.gh.created == []
    assert rig.request("ghost", "pause").status_code == 404
    ok = rig.request("analytics", "pause")
    assert ok.status_code == 202, ok.text
    assert rig.pc.operations() == [ok.json()["operation"]]
