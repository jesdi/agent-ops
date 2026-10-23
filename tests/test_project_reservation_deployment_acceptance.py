"""Acceptance tests for ticket 20 (project-onboarding): a pending enable request
holds a free reservation through GitHub publication and box application, driven
through the CONTROL seam (ProjectControl) and the dispatcher's claim round.

Spec: specs/project-onboarding/spec.md S36, S37 (R16, R17).
Ticket: .agent/tickets/20-reservation-deployment-handoff.md.

Interface these tests define (the ticket leaves its shape open):
- `ProjectControl(state_dir=<the dispatcher's state_dir>, ...)` reads the
  unfinished tasks from that directory and counts enabled projects from the
  PR-backed configuration (`github.main()`), never from a runtime flag.
- `request_enabled(name, True)` records the request. `advance(op)` admits it
  only when `unfinished tasks + reservations held` leaves room for the project's
  reservation (10 places, at most 10 enabled projects). Until admitted the
  request is `phase="blocked"` with a `blocker` naming the reservation capacity
  it waits for, `pr is None`, and `pending_change(name)` shows the same blocker.
  It stays resumable: a later `advance` admits it once room exists.
- An admitted request holds its place from admission until its deployment is
  verified complete or the operation fails. While held, `dispatcher.main.run_pass`
  on the same `state_dir` denies every claim that would consume the place, and
  allows claims that do not. Another request is admitted against the holds of
  the earlier ones.
- `abandon(operation_id)` fails an operation: it first closes the still-open PR
  with `github.close_pr(number)` and confirms through `find_pr(branch)["state"]`
  (`"open"` | `"closed"`, plus `merged`/`merge_sha` once merged) that it is closed
  unmerged; only then is the operation `phase="failed"` and its hold released.
  An open PR that cannot be confirmed closed keeps the hold: a merge could still
  land and promise capacity that is gone (R17).
"""
import threading
from dataclasses import replace

import pytest

from project_control import ConflictingConfirmation, ProjectControl
from dispatcher import claims, state
from dispatcher.state import NO_SLOT, Stage, load_all, save
from tests.test_main import FakeSessions, patch_usage, patch_workspace
from tests.test_project_control_pr_deployment_acceptance import Deployer, Github
from tests.test_project_reservations_acceptance import (Gh, claimed_for, issues, open_total,
                                                        project, run, unfinished, world)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)


def entry(key, enabled):
    return {"repositories": [{"repo": f"example/{key}-0"}], "board": {"owner": "example", "number": 1},
            "models": "inherit", "desired_enabled": enabled}


class Setup:
    def __init__(self, tmp_path, enabled, paused):
        self.tmp = tmp_path
        self.enabled, self.paused = list(enabled), list(paused)
        self.cfg = world(tmp_path, self._projects(self.enabled))
        self.gh = Github(self.cfg.state_dir)
        self.gh.config = {"projects": {**{k: entry(k, True) for k in self.enabled},
                                       **{k: entry(k, False) for k in self.paused}}}
        self._closable_prs()
        self.dep = Deployer(self.cfg.state_dir, self.gh)
        self.dep.live_config = {"projects": {k: dict(v) for k, v in self.gh.config["projects"].items()}}
        self.control = self.fresh()

    def _closable_prs(self):
        gh, plain = self.gh, self.gh.find_pr

        def find_pr(branch):
            pr = plain(branch)
            if pr:
                pr["state"] = gh.prs[pr["number"]].get("state", "open")
            return pr

        def close_pr(number):
            gh.prs[number]["state"] = "closed"
        gh.find_pr, gh.close_pr = find_pr, close_pr

    def _projects(self, enabled):
        return [project(self.tmp, k) for k in enabled] + \
               [project(self.tmp, k, enabled=False) for k in getattr(self, "paused", [])]

    def fresh(self):
        return ProjectControl(state_dir=self.cfg.state_dir, github=self.gh, deployer=self.dep)

    def applied_cfg(self, *newly_enabled):
        self.enabled += newly_enabled
        self.paused = [p for p in self.paused if p not in newly_enabled]
        return replace(self.cfg, projects=self._projects(self.enabled))

    def advance(self, *ops, times=3):
        for _ in range(times):
            for op in ops:
                self.control.advance(op)

    def finish_one(self, owner):
        ts = next(t for t in load_all(self.cfg.state_dir)
                  if t.target == owner and t.stage not in state.TERMINAL_STAGES)
        save(self.cfg.state_dir, replace(ts, stage=Stage.DONE, slot=NO_SLOT))


def blocker(s, op):
    return s.control.status(op)["blocker"].lower()


def test_s36_a_new_reservation_waits_for_work_to_drain_then_completes_unstolen(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing"])
    tasks = unfinished(s.cfg, "analytics", 10)
    op = s.control.request_enabled("billing", True)
    s.advance(op)
    status = s.control.status(op)
    assert status["phase"] == "blocked" and "reservation" in status["blocker"].lower()
    assert status["pr"] is None and s.gh.created == [] and s.gh.merges == []
    assert "reservation" in s.control.pending_change("billing")["blocker"].lower()
    assert {t.issue for t in load_all(s.cfg.state_dir) if t.stage not in state.TERMINAL_STAGES} \
        == {t.issue for t in tasks}, "waiting must not cancel analytics work"
    assert s.gh.lock_free and all(s.gh.lock_free)

    s.finish_one("analytics")
    s.dep.auto = False
    s.advance(op, times=4)
    assert s.control.status(op)["phase"] == "merged"
    stealer = Gh({"analytics": issues(1, 3)})
    run(s.cfg, stealer)
    assert claimed_for(stealer, "analytics") == [], "a claim took billing's admitted place"
    assert open_total(s.cfg) == 9

    s.dep.auto = True
    s.advance(op)
    assert s.control.status(op)["phase"] == "complete"
    after = Gh({"analytics": issues(10, 2), "billing": issues(20, 1)})
    run(s.applied_cfg("billing"), after)
    assert claimed_for(after, "billing") == [20]
    assert claimed_for(after, "analytics") == []


def test_an_admitted_place_survives_the_github_wait_while_unrelated_claims_continue(tmp_path):
    s = Setup(tmp_path, ["analytics", "catalog"], ["billing"])
    unfinished(s.cfg, "analytics", 8)
    s.gh.check_state = {"state": "pending", "failing": []}
    op = s.control.request_enabled("billing", True)
    s.advance(op)
    status = s.control.status(op)
    assert status["pr"] is not None and status["phase"] != "blocked", status
    d = Gh({"analytics": issues(1, 2), "catalog": issues(50, 1)})
    run(s.cfg, d)
    assert claimed_for(d, "analytics") == []
    assert claimed_for(d, "catalog") == [50]
    assert open_total(s.cfg) == 9


def test_a_failed_operation_releases_only_its_own_hold_and_a_restart_releases_nothing(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing", "payroll"])
    unfinished(s.cfg, "analytics", 8)
    s.gh.check_state = {"state": "pending", "failing": []}
    billing, payroll = s.control.request_enabled("billing", True), s.control.request_enabled("payroll", True)
    s.advance(billing, payroll)
    assert s.control.status(billing)["pr"] and s.control.status(payroll)["pr"]
    seen = s.control.status(payroll)
    del s.control  # the terminal and its process are gone
    s.control = s.fresh()
    assert s.control.status(payroll) == seen
    d = Gh({"analytics": issues(1, 2)})
    run(s.cfg, d)
    assert claimed_for(d, "analytics") == []

    s.control.abandon(billing)
    assert s.control.status(billing)["phase"] == "failed"
    assert s.gh.prs[s.control.status(billing)["pr"]["number"]].get("state") == "closed"
    assert s.gh.merges == [], "abandoning must close the PR, never merge it"
    d = Gh({"analytics": issues(10, 2)})
    run(s.cfg, d)
    assert claimed_for(d, "analytics") == [10], "billing's place frees, payroll's stays held"
    s.gh.check_state = {"state": "passing", "failing": []}
    s.advance(payroll)
    assert s.control.status(payroll)["phase"] == "complete"


def _abandon_refused_and_place_kept(s, op, want_phase):
    with pytest.raises(ConflictingConfirmation):
        s.control.abandon(op)
    assert s.control.status(op)["phase"] == want_phase
    d = Gh({"analytics": issues(1, 2)})
    run(s.cfg, d)
    assert claimed_for(d, "analytics") == [], "abandoning a merged enable released its place"
    assert open_total(s.cfg) == 9


def test_abandoning_a_merged_enable_awaiting_deployment_is_refused_and_keeps_its_place(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing"])
    unfinished(s.cfg, "analytics", 9)
    s.dep.auto = False
    op = s.control.request_enabled("billing", True)
    s.advance(op)
    assert s.control.status(op)["phase"] == "merged"
    _abandon_refused_and_place_kept(s, op, "merged")


def test_abandoning_an_enable_whose_merge_outcome_is_uncertain_is_refused_and_keeps_its_place(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing"])
    unfinished(s.cfg, "analytics", 9)
    plain = s.gh.merge

    def lost(number, head_sha):
        plain(number, head_sha)
        raise ConnectionError("response lost after the merge")
    s.gh.merge = lost
    op = s.control.request_enabled("billing", True)
    with pytest.raises(ConnectionError):
        s.control.advance(op)
    _abandon_refused_and_place_kept(s, op, s.control.status(op)["phase"])


def test_s37_simultaneous_enables_from_nine_never_promise_eleven(tmp_path):
    nine = [f"p{i}" for i in range(9)]
    s = Setup(tmp_path, nine, ["billing", "payroll"])
    ops = [s.control.request_enabled("billing", True), s.control.request_enabled("payroll", True)]
    s.advance(*ops, times=4)

    def enabled_in(config):
        return [k for k, v in config["projects"].items() if v["desired_enabled"]]
    for pr in s.gh.prs.values():
        assert len(enabled_in(pr["config"])) <= 10
    assert len(enabled_in(s.gh.config)) == 10
    assert len(enabled_in(s.dep.live_config)) == 10
    statuses = sorted(s.control.status(op)["phase"] for op in ops)
    assert statuses == ["blocked", "complete"]
    waiting = next(op for op in ops if s.control.status(op)["phase"] == "blocked")
    assert "reservation" in blocker(s, waiting)
    winner = "billing" if waiting == ops[1] else "payroll"
    assert winner in enabled_in(s.gh.config), "the other confirmed change must survive"


def test_a_claim_in_flight_cannot_take_the_place_an_admission_just_took(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing"])
    unfinished(s.cfg, "analytics", 9)
    op = s.control.request_enabled("billing", True)
    inside, proceed = threading.Event(), threading.Event()

    class Paused(Gh):
        def candidates(self, target):
            inside.set()
            proceed.wait(5)
            return super().candidates(target)
    d = Paused({"analytics": issues(1, 1)})
    t = threading.Thread(target=lambda: run(s.cfg, d))
    t.start()
    try:
        assert inside.wait(5)
        s.control.advance(op)  # admits billing while the pass is between ranking and claiming
    finally:
        proceed.set()
        t.join(10)
    assert s.control.status(op)["phase"] != "blocked"
    assert claimed_for(d, "analytics") == [], "the pass must recheck the hold under the claim lock"
    assert open_total(s.cfg) == 9


def test_a_claim_that_took_the_last_place_makes_a_later_request_wait(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing"])
    unfinished(s.cfg, "analytics", 9)
    op = s.control.request_enabled("billing", True)
    d = Gh({"analytics": issues(1, 1)})
    run(s.cfg, d)
    assert claimed_for(d, "analytics") == [1] and open_total(s.cfg) == 10
    s.advance(op)
    assert s.control.status(op)["phase"] == "blocked"
    assert "reservation" in blocker(s, op)
    assert s.gh.merges == [] and s.gh.created == []


def test_overlapping_admissions_for_the_last_place_promise_it_to_exactly_one(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing", "payroll"])
    unfinished(s.cfg, "analytics", 9)
    billing, payroll = s.control.request_enabled("billing", True), s.control.request_enabled("payroll", True)
    first_in, second_reading, release = threading.Event(), threading.Event(), threading.Event()
    seen = []
    plain = s.gh.main

    def main():
        me = threading.current_thread()
        if me not in seen:
            seen.append(me)
            if len(seen) == 1:
                first_in.set()  # the first admission has read the configuration, not yet saved its hold
                release.wait(5)
            else:
                second_reading.set()
        return plain()
    s.gh.main = main
    first = threading.Thread(target=lambda: s.control.advance(billing))
    second = threading.Thread(target=lambda: s.control.advance(payroll))
    first.start()
    try:
        assert first_in.wait(5)
        second.start()
        second_reading.wait(1)  # without a shared admission lock the second admission reads now
    finally:
        release.set()
        first.join(10)
        second.join(10)
    phases = sorted(s.control.status(op)["phase"] for op in (billing, payroll))
    assert phases[0] == "blocked" and phases[1] != "blocked", phases
    d = Gh({"analytics": issues(100, 3)})
    run(s.cfg, d)
    assert claimed_for(d, "analytics") == [], "the one admitted place must stay held"
    assert open_total(s.cfg) == 9


def test_the_updater_contract_refuses_a_configuration_that_overcommits_places_and_accepts_it_after_room_appears(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing"])
    unfinished(s.cfg, "analytics", 10)
    sd = s.cfg.state_dir
    assert claims.configuration_fits(sd, ["analytics"])
    assert not claims.configuration_fits(sd, ["analytics", "billing"]), "the eleventh place was promised"
    s.finish_one("analytics")
    assert claims.configuration_fits(sd, ["analytics", "billing"])
    assert not claims.configuration_fits(sd, [f"p{i}" for i in range(11)]), "eleven enabled projects"


def test_the_updater_contract_counts_held_reservations(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing", "payroll"])
    unfinished(s.cfg, "analytics", 8)
    s.gh.check_state = {"state": "pending", "failing": []}
    ops = [s.control.request_enabled("billing", True), s.control.request_enabled("payroll", True)]
    s.advance(*ops)
    assert all(s.control.status(op)["pr"] for op in ops)
    assert claims.configuration_fits(s.cfg.state_dir, ["analytics"])
    unfinished(s.cfg, "analytics", 1)
    assert not claims.configuration_fits(s.cfg.state_dir, ["analytics"]), "held places were forgotten"
    s.control.abandon(ops[0])
    assert claims.configuration_fits(s.cfg.state_dir, ["analytics"])


def test_a_stale_admitted_enable_is_refreshed_and_the_other_confirmed_change_survives(tmp_path):
    s = Setup(tmp_path, ["analytics"], ["billing", "catalog"])
    unfinished(s.cfg, "analytics", 7)
    gh = s.gh

    def update_pr(number, expected_head, base_sha, config):
        pr = gh.prs[number]
        assert expected_head == pr["head_sha"], "refreshing a head nobody confirmed"
        pr.update(base_sha=base_sha, config=config, head_sha=pr["head_sha"] + "r")
        return dict(pr)
    gh.update_pr = update_pr
    gh.check_state = {"state": "pending", "failing": []}
    op = s.control.request_enabled("billing", True)
    s.advance(op)
    number = s.control.status(op)["pr"]["number"]
    assert s.control.status(op)["phase"] != "blocked"
    gh.advance_main({"catalog": entry("catalog", True)})  # another confirmed change lands first
    s.advance(op)
    assert s.control.status(op)["pr"]["number"] == number and s.gh.merges == []
    gh.check_state = {"state": "passing", "failing": []}
    s.advance(op, times=4)
    assert s.control.status(op)["phase"] == "complete"
    enabled = {k for k, v in gh.config["projects"].items() if v["desired_enabled"]}
    assert {"analytics", "billing", "catalog"} <= enabled, "the refresh dropped the other confirmed change"
