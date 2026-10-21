"""Acceptance tests for ticket 22 (project-onboarding): completing onboarding
with a verified deployment and a readiness status, driven through the
PROJECT_CLI seam (`agent-ops project add|enable|pause|status`) over the real
`ProjectControl` and the web's `/api/projects`.

Spec: specs/project-onboarding/spec.md S03, S49, S50 (R2, R3, R12, R22).
Ticket: .agent/tickets/22-verified-onboarding-completion.md.

Interface these tests define on top of tickets 14-19 (the ticket leaves its
shape open):
- `GET /api/projects` tabs also carry `tasks`: the number of tasks of that
  project (any stage).
- `ProjectControl.completion(operation_id, web) -> dict` is the real completion
  check, a read. `web.projects()` is the web's applied project view (the tabs of
  `GET /api/projects`). The host also answers `has_checkout(path) -> bool`
  (the separate target checkout exists on the box) and
  `unresolved_inputs() -> [str]` (production inputs of Q1-Q5 still missing).
  Keys: `complete` (bool), `applied_revision` (the deployed merge revision, None
  until verified), `blockers` ([str], each naming the failed check), `project`
  ({"state", "tasks", "tab"}: read from the web's applied view, not from the
  plan), `board` ({"owner", "number"} of the versioned configuration),
  `repositories` ([{"repo", "checkout", "present"}]).
- `box.status(name)["completion"]` is that dict. When present, `project status`
  prints a `Completion:` section with the lines
  `Applied revision: <revision>`, `Project: <name> - paused|enabled, <n> tasks`,
  `Board: <owner>/<number>`, `Repository: <repo> -> <checkout>` and
  `Blocked: <blocker>` per blocker, and `Onboarding: complete` additionally needs
  `completion["complete"]`.
- `project add` prints `Operation: <id>` for what `box.confirm(plan)` or, on a
  rerun, `box.resume(name)` returns.
"""
import io
from copy import deepcopy

import pytest
import yaml

from tests.test_main import FakeSessions, patch_usage, patch_workspace
from tests.test_project_authorization_write_verification_acceptance import (
    BOARD_OK, repo_state, status as auth_status)
from tests.test_project_provisioning_verification_acceptance import Host as BaseHost
from tests.test_project_reservations_acceptance import Gh, issues, open_total, run
from tests.test_project_tabs_controls_acceptance import (
    FRONT, BACK, Rig, TermBox as ControlBox, cli, entry, write_live)

EXISTING = "portfolio_eval"
EXISTING_REPO = "example/portfolio_eval"
AGENT_OPS = "example/agent-ops"
APP = "/srv/agent-ops/app"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)


class Host(BaseHost):
    def __init__(self):
        super().__init__()
        self.unresolved, self.removed = [], set()

    def has_checkout(self, path):
        return path in {p for _, p in self.clone_calls} and path not in self.removed

    def unresolved_inputs(self):
        return list(self.unresolved)


class WebView:
    def __init__(self, rig):
        self.rig = rig

    def projects(self):
        return list(self.rig.tabs().values())


class Box(ControlBox):
    """The box over SSH for the whole journey: reads and writes reach the real control service."""

    def __init__(self, rig, host):
        super().__init__(rig)
        self.host, self.ops, self.members, self.web = host, {}, {}, WebView(rig)

    def inspect(self, repo):
        return dict(setup_cmd="pnpm install", verify_cmd="pnpm e2e", gate_cmd="make gate",
                    board=dict(owner="example", number=8), compose_services=["postgres"],
                    secret_refs=[])

    def board_state(self, ref):
        return self.rig.boards.board_state(ref)

    def confirm(self, plan):
        plan = deepcopy(plan)
        name = plan["name"]
        for r in plan["repositories"]:
            r["clone_path"] = str(self.rig.tmp / "clones" / r["repo"].split("/")[1])
        plan["locations"] = dict(skills="/srv/agent-ops/skills", storage=f"/srv/agent-ops/state/{name}",
                                 target=f"/srv/agent-ops/{name}", app=APP)
        self.members[name] = [r["repo"] for r in plan["repositories"]]
        self.ops[name] = self.rig.pc.confirm(plan)
        return self.ops[name]

    def resume(self, name):
        return self.ops[name]

    def status(self, name):
        op = self.ops[name]
        members = self.members[name]
        report = auth_status({r: repo_state() for r in members}, BOARD_OK)
        report["provisioning"] = self.rig.pc.status(op)["provisioning"]
        report["completion"] = self.rig.pc.completion(op, web=self.web)
        return report


def make_rig(tmp_path):
    host = Host()
    rig = Rig(tmp_path, host=host)
    rig.boards.cards = []  # a fresh dedicated board: no cards of other repositories
    rig.gh.config["projects"] = {EXISTING: entry(tmp_path, [EXISTING_REPO], True)}
    write_live(rig.path, rig.state, rig.gh.config)
    return rig, host, Box(rig, host)


def onboard(rig, box, name, *repos):
    code, out, err = cli(box, "project", "add", name, *repos, stdin="yes\n")
    assert code == 0, err
    op = box.ops[name]
    assert rig.pc.provision(op, slot=1)["state"] == "passed"
    rig.advance(op)
    assert rig.pc.status(op)["phase"] == "merged"
    rig.dep.roll_out()
    rig.advance(op)
    assert rig.pc.status(op)["phase"] == "complete"
    return op, out


def enable(rig, box, name):
    assert cli(box, "project", "enable", name, stdin="yes\n")[0] == 0
    op = rig.pc.pending_change(name)["operation"]
    rig.advance(op)
    rig.dep.roll_out()
    rig.advance(op)
    assert rig.tabs()[name]["state"] == "enabled"


def report(box, name):
    code, out, err = cli(box, "project", "status", name)
    assert code == 0, err
    return out


def completion_of(out):
    return "\n".join(l for l in out.splitlines() if l.startswith(("Applied", "Project", "Board",
                                                                 "Repository", "Blocked")))


def test_s03_completion_verifies_the_deployed_revision_checkout_and_paused_empty_tab(tmp_path):
    rig, host, box = make_rig(tmp_path)
    code, out, err = cli(box, "project", "add", "analytics", "example/analytics", stdin="yes\n")
    assert code == 0, err
    op = box.ops["analytics"]
    assert rig.pc.provision(op, slot=1)["state"] == "passed"
    rig.advance(op)
    merged = report(box, "analytics")  # merged but not yet deployed: no success
    assert "Onboarding: incomplete" in merged and "Applied revision" not in merged
    rig.dep.roll_out()
    rig.advance(op)
    done = report(box, "analytics")
    assert f"Applied revision: {rig.gh.sha}" in done
    assert "Project: analytics - paused, 0 tasks" in done
    assert "Board: example/8" in done
    assert f"Repository: example/analytics -> {tmp_path}/clones/analytics" in done
    assert "Onboarding: complete" in done
    assert rig.tabs()["analytics"]["tasks"] == 0 and rig.tabs()["analytics"]["state"] == "paused"


@pytest.mark.parametrize("breakage, named", [("stale web", "web"), ("missing checkout", "checkout")])
def test_completion_reads_the_applied_web_view_and_box_checkout_not_the_plan(tmp_path, breakage, named):
    rig, host, box = make_rig(tmp_path)
    if breakage == "stale web":  # the revision is applied but the web still serves the old file
        rig.dep.roll_out = lambda: setattr(rig.dep, "revision", rig.gh.sha)
    code, out, err = cli(box, "project", "add", "analytics", "example/analytics", stdin="yes\n")
    assert code == 0, err
    op = box.ops["analytics"]
    rig.pc.provision(op, slot=1)
    rig.advance(op)
    rig.dep.roll_out()
    rig.advance(op)
    assert rig.pc.status(op)["phase"] == "complete"  # the plan and the merge say done
    if breakage == "missing checkout":
        host.removed = {p for _, p in host.clone_calls}
    done = report(box, "analytics")
    blocked = [l for l in done.splitlines() if l.startswith("Blocked:")]
    assert blocked and named in blocked[0].lower() and "analytics" in blocked[0]
    assert "Onboarding: incomplete" in done and "Onboarding: complete" not in done


def test_a_rerun_reports_the_same_operation_instead_of_starting_another(tmp_path):
    rig, host, box = make_rig(tmp_path)
    op, first = onboard(rig, box, "analytics", "example/analytics")
    assert f"Operation: {op}" in first
    code, again, err = cli(box, "project", "add", "analytics", "example/analytics", stdin="yes\n")
    assert code == 0, err
    assert f"Operation: {op}" in again
    assert rig.pc.operations().count(op) == 1 and len(rig.gh.created) == 1
    assert "Applied revision" in report(box, "analytics")


def test_unresolved_production_inputs_are_blockers_not_a_hidden_success(tmp_path):
    rig, host, box = make_rig(tmp_path)
    host.unresolved = ["Q5: GitHub App installation id for example"]
    onboard(rig, box, "analytics", "example/analytics")
    done = report(box, "analytics")
    assert "Blocked: " in done and "Q5: GitHub App installation id" in done
    assert "Onboarding: incomplete" in done and "Onboarding: complete" not in done
    host.unresolved = []
    assert "Onboarding: complete" in report(box, "analytics")


def test_s49_agent_ops_is_onboarded_paused_beside_portfolio_eval(tmp_path):
    rig, host, box = make_rig(tmp_path)
    onboard(rig, box, "agent-ops", AGENT_OPS)
    tabs = rig.tabs()
    assert list(tabs) == [EXISTING, "agent-ops"]
    assert tabs["agent-ops"]["state"] == "paused" and tabs[EXISTING]["state"] == "enabled"
    done = report(box, "agent-ops")
    assert "Onboarding: complete" in done and "Project: agent-ops - paused, 0 tasks" in done
    assert "Board: example/8" in done  # its dedicated board, not portfolio_eval's 7
    assert rig.config().projects[0].board_number == 7
    checkouts = {p for _, p in host.clone_calls}
    assert len(checkouts) == 1 and APP not in checkouts
    assert all(not c.startswith(APP) for c in checkouts)
    assert f"-> {APP}" not in done


def test_s50_both_projects_claim_one_task_each_after_enabling(tmp_path, monkeypatch):
    from dispatcher import main
    made = []
    inner = main.create_workspace

    def recording(target, issue, **kw):
        wt = inner(target, issue, **kw)
        made.append((target.name, issue, wt, str(target.worktrees_path)))
        return wt

    monkeypatch.setattr(main, "create_workspace", recording)
    rig, host, box = make_rig(tmp_path)
    onboard(rig, box, "agent-ops", AGENT_OPS)
    assert "Onboarding: complete" in report(box, "agent-ops")
    enable(rig, box, "agent-ops")
    done = report(box, "agent-ops")
    assert "Project: agent-ops - enabled, 0 tasks" in done and "Onboarding: complete" in done
    live = yaml.safe_load(rig.path.read_text())
    live["capacity"] = 2  # S50: exactly two execution slots
    rig.path.write_text(yaml.safe_dump(live))
    assert rig.config().capacity == 2
    gh = Gh({EXISTING: issues(100, 1), "agent-ops": issues(200, 2)})
    sessions = run(rig.config(), gh, FakeSessions())
    # one task each: the second agent-ops issue finds no slot
    assert sorted(gh.claimed_by) == [("agent-ops", 200), (EXISTING, 100)]
    # each task got its own sandbox workspace, inside its own project, and its session ran there
    by_project = {name: wt for name, _, wt, _ in made}
    assert sorted(by_project) == ["agent-ops", EXISTING] and len(set(by_project.values())) == 2
    for name, _, wt, root in made:
        assert wt.startswith(root)
    assert len({root for *_, root in made}) == 2
    assert by_project["agent-ops"] != by_project[EXISTING]
    assert sorted((getattr(t, "key", t), i) for t, i in sessions.spawn_calls) == [("agent-ops", 200), (EXISTING, 100)]
    assert open_total(rig.config()) == 2
    assert rig.tabs()["agent-ops"]["tasks"] == 1 and rig.tabs()[EXISTING]["tasks"] == 1


def test_a_frontend_backend_project_is_one_tab_with_both_checkouts_and_one_task_place(tmp_path, monkeypatch):
    from dispatcher import main
    made = []
    inner = main.create_workspace

    def recording(target, issue, **kw):
        wt = inner(target, issue, **kw)
        made.append((target.name, issue, wt))
        return wt

    monkeypatch.setattr(main, "create_workspace", recording)
    rig, host, box = make_rig(tmp_path)
    onboard(rig, box, "shop", FRONT, BACK)
    tabs = rig.tabs()
    assert list(tabs) == [EXISTING, "shop"] and sorted(tabs["shop"]["repositories"]) == [BACK, FRONT]
    done = report(box, "shop")
    assert f"Repository: {FRONT} -> {tmp_path}/clones/shop-frontend" in done
    assert f"Repository: {BACK} -> {tmp_path}/clones/shop-backend" in done
    assert "Project: shop - paused, 0 tasks" in done and "Onboarding: complete" in done
    enable(rig, box, "shop")
    gh = Gh({"shop": issues(300, 1)})
    run(rig.config(), gh, FakeSessions())
    assert gh.claimed_by == [("shop", 300)] and open_total(rig.config()) == 1  # counted once, not per repository
    # scoped execution: the task works in one workspace of the shop project, under one of its checkouts
    [(name, issue, wt)] = made
    assert (name, issue) == ("shop", 300)
    assert any(wt.startswith(str(tmp_path / "clones" / d)) for d in ("shop-frontend", "shop-backend"))
    assert rig.tabs()["shop"]["tasks"] == 1
