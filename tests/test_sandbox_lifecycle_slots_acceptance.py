"""Acceptance tests for ticket 10 (project-onboarding): sandbox lifecycle and
shared execution slots, driven through dispatcher.main.run_pass.

Spec: specs/project-onboarding/spec.md S42, S43, S45 (R19, R20). Ticket:
.agent/tickets/10-sandbox-lifecycle-slots.md.

Interface these tests define (the ticket leaves its shape open):
- `main.Deps.sandbox`: the box's `TaskSandbox` (dispatcher/sandbox.py). When
  set, run_pass owns the lifecycle of every task whose execution ref
  (`sandbox_runtime.execution_ref(clone, issue)`) has a plan there: it parks
  (stops all containers, keeps volumes) when the task's session ends or the
  task is terminal, resumes before a woken task's session is relaunched, and
  cleans up when `_flush_finished` retires a task. A task without a plan is
  not touched.
- A task releases its execution slot only after the stop is confirmed: while a
  container could not be stopped the task still occupies it, and the stop is
  retried on the next pass. A cleanup that fails keeps the task record (so it
  is retried), is reported as a `cleanup-failed` event, and never touches
  another task's resources.
- `main.Deps.verification`: onboarding verification requests. `pending()` lists
  operation ids waiting for a slot; `start(operation_id, slot)` is called once
  the operation is admitted to a free execution slot; `is_running(operation_id)`
  tells whether its process still runs (it survives a dispatcher restart).
  Verification holds a slot but never a TaskState (no unfinished place).
- `state.allocations(state_dir)`: the one execution allocation view shared by
  tasks, triage and verification: a list of objects with `owner_kind`
  ("task" | "triage" | "verification"), `owner_id` and `slot`. Slots are unique
  across owners; an allocation of a verification whose process still runs is
  kept across passes and is dropped once the process is gone.
"""
import json
from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import eventlog, herdr, intents as intents_mod, state, triage
from dispatcher.sandbox import EngineError, SandboxRequest, TaskSandbox
from dispatcher.sandbox_runtime import execution_ref
from dispatcher.state import NO_SLOT, PARK_HUMAN, PARK_WAKE, SessionRecord, Stage, TaskState

from tests.test_main import FakeSessions, deps, patch_usage, patch_workspace
from tests.test_project_reservations_acceptance import (
    Gh, issues, live_slots, open_total, project, unfinished, world)
from tests.test_task_sandbox_acceptance import Box, compose, make_clone

REPO = "example/analytics-0"
OLD = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "remove_workspace", lambda *a, **k: None)


class FlakyBox(Box):
    """Fails to stop a service, or to remove a plan, while told to."""

    def __init__(self):
        super().__init__()
        self.fail_stop, self.fail_remove = set(), set()

    def stop(self, plan, name):
        if self.specs[name][1].service in self.fail_stop:
            raise EngineError(f"{name} did not stop")
        super().stop(plan, name)

    def remove(self, plan):
        if plan.ref in self.fail_remove:
            raise EngineError(f"{plan.ref} was not removed")
        super().remove(plan)


class Verifier:
    def __init__(self, pending=(), running=()):
        self.waiting, self.running = list(pending), set(running)
        self.started = []

    def pending(self):
        return list(self.waiting)

    def start(self, operation_id, slot):
        self.started.append((operation_id, slot))
        self.waiting.remove(operation_id)
        self.running.add(operation_id)

    def is_running(self, operation_id):
        return operation_id in self.running


class Watching(FakeSessions):
    """Remembers which containers were running when a session was relaunched."""

    def __init__(self, box, **kw):
        super().__init__(**kw)
        self.box, self.running_at_resume = box, []

    def resume(self, *args, **kw):
        self.running_at_resume.append(set(self.box.running))
        super().resume(*args, **kw)


class Lab:
    """analytics with one real clone, a fake rootless box and task sandboxes."""

    def __init__(self, tmp_path, capacity=2, box=None):
        self.box = box or Box()
        self.clone = make_clone(tmp_path, "analytics", compose())
        proj = project(tmp_path, "analytics")
        repo = replace(proj.repositories[0], clone_path=self.clone)
        self.cfg = world(tmp_path, [replace(proj, repositories=[repo])], capacity=capacity)
        self.requests = {}
        self.sandbox = TaskSandbox(self.box, tmp_path / "sandboxes", self.requests)
        self.gh = Gh({})

    def ref(self, issue):
        return execution_ref(self.clone, issue)

    def container(self, issue, service=""):
        return self.box.container(self.ref(issue), service)

    def volume(self, issue):
        return self.box.volume_of(self.container(issue, "postgres"))

    def task(self, issue, *, stage=Stage.IMPLEMENT, slot=0, started=True, **fields):
        """A task with an owned sandbox. By default its containers run."""
        ref = self.ref(issue)
        self.requests[ref] = SandboxRequest(
            project="analytics", repo=REPO, clone=self.clone,
            services=("postgres", "redis"), setup="true", gate="true", run_gate=False)
        plan = self.sandbox.prepare(ref)
        if started:
            assert self.sandbox.start(ref).ready
        wt = Path(plan.checkout)
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        ts = TaskState(issue=issue, target="analytics", repo=REPO, stage=stage, slot=slot,
                       worktree=str(wt), branch=f"agent/task-{issue}", title="t",
                       updated_at=fields.pop("updated_at", "2026-07-21T00:00:00+00:00"),
                       **fields)
        state.save(self.cfg.state_dir, ts)
        state.write_session(self.cfg.state_dir, "analytics", issue,
                            SessionRecord("sid", ts.continued_stage.value), repo=REPO)
        return wt

    def load(self, issue):
        return state.load(self.cfg.state_dir, "analytics", issue, repo=REPO)

    def run(self, sess=None, gh=None, verifier=None):
        sess = sess or Watching(self.box)
        sess.alive_set = {t.issue for t in live_slots(self.cfg)}
        d = deps(gh or self.gh, sess)
        d.sandbox = self.sandbox
        if verifier is not None:
            d.verification = verifier
        main.run_pass(self.cfg, d)
        return sess


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def park_intent(lab, issue):
    intents_mod.write_intent(lab.cfg.state_dir, "park", "analytics", issue, {}, "op", 1,
                             repo=REPO)


def running_services(lab, issue):
    return {n for n in lab.box.running if lab.box.specs[n][0].ref == lab.ref(issue)}


# --- S42 -------------------------------------------------------------------

def test_s42_park_stops_services_keeps_the_record_and_resume_sees_it(lab, monkeypatch):
    wt = lab.task(12)
    lab.box.exec(lab.container(12, "postgres"), "sql insert invoice-12")
    assert running_services(lab, 12)
    park_intent(lab, 12)

    lab.run(Watching(lab.box, alive={12}))

    t = lab.load(12)
    assert t.park == PARK_HUMAN and t.slot == NO_SLOT
    assert running_services(lab, 12) == set()
    assert lab.box.data[lab.volume(12)] == ["invoice-12"]

    # woken, but the usage gate denies the relaunch: nothing restarts
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    state.save(lab.cfg.state_dir, replace(t, park=PARK_WAKE))
    patch_usage(monkeypatch, util=0.99)
    sess = lab.run(Watching(lab.box))
    assert sess.resumed == [] and running_services(lab, 12) == set()

    patch_usage(monkeypatch, util=0.2)
    sess = lab.run(Watching(lab.box))

    assert [i for i, *_ in sess.resumed] == [12]
    assert {lab.box.specs[n][1].service for n in sess.running_at_resume[0]
            if n in running_services(lab, 12)} >= {"postgres", "redis"}
    assert lab.box.exec(lab.container(12, "postgres"), "sql list").output == "invoice-12"
    assert lab.load(12).slot != NO_SLOT


def test_park_releases_the_slot_only_after_the_stop_is_confirmed(tmp_path):
    lab = Lab(tmp_path, capacity=1, box=FlakyBox())
    lab.task(12)
    lab.gh.cands = {"analytics": issues(99, 1)}
    lab.box.fail_stop = {"postgres"}
    park_intent(lab, 12)

    lab.run(Watching(lab.box, alive={12}))

    assert lab.gh.claimed == []          # the still-running postgres keeps the only slot
    assert any(a.owner_kind == "task" and a.owner_id.endswith("12")
               for a in state.allocations(lab.cfg.state_dir))

    lab.box.fail_stop = set()
    lab.run()

    assert running_services(lab, 12) == set()
    assert lab.gh.claimed == [99]


# --- S43 -------------------------------------------------------------------

def test_s43_cleanup_removes_only_the_eligible_tasks_resources(lab):
    lab.task(12, stage=Stage.DONE, slot=NO_SLOT, updated_at=OLD, started=False)
    fresh = lab.task(13, stage=Stage.DONE, slot=NO_SLOT, started=False,
                     updated_at=main._now())
    lab.task(15, stage=Stage.SPEC, slot=NO_SLOT, park=PARK_HUMAN, started=False)
    lab.box.exec(lab.container(15, "postgres"), "sql insert invoice-15")
    doomed = lab.sandbox._plan_for(lab.ref(12)).checkout

    lab.run()

    assert lab.load(12) is None
    assert lab.ref(12) not in lab.box.plans and not Path(doomed).exists()
    # not terminal long enough (no new retention period), and not terminal at all
    assert lab.ref(13) in lab.box.plans and lab.load(13) is not None and fresh.exists()
    assert lab.ref(15) in lab.box.plans and Path(lab.sandbox._plan_for(lab.ref(15)).checkout).exists()
    assert lab.box.data[lab.volume(15)] == ["invoice-15"]


def test_failed_cleanup_is_visible_and_touches_nothing_else(tmp_path):
    lab = Lab(tmp_path, box=FlakyBox())
    lab.task(12, stage=Stage.FAILED, slot=NO_SLOT, updated_at=OLD, started=False)
    lab.task(15, stage=Stage.SPEC, slot=NO_SLOT, park=PARK_HUMAN, started=False)
    lab.box.fail_remove = {lab.ref(12)}

    lab.run()

    assert lab.load(12) is not None                       # kept so cleanup is retried
    failed = [e for e in eventlog.read_tail(lab.cfg.state_dir) if e["event"] == "cleanup-failed"]
    assert [e["issue"] for e in failed] == [12]
    assert lab.ref(15) in lab.box.plans and lab.load(15) is not None

    lab.box.fail_remove = set()
    lab.run()

    assert lab.load(12) is None and lab.ref(12) not in lab.box.plans
    assert lab.ref(15) in lab.box.plans


# --- no Compose service outside accounting ---------------------------------

@pytest.mark.parametrize("case", ["kill-intent", "terminal-orphan"])
def test_services_do_not_outlive_their_agent_session(lab, case):
    if case == "kill-intent":
        lab.task(12)
        intents_mod.write_intent(lab.cfg.state_dir, "kill", "analytics", 12, {}, "op", 1,
                                 repo=REPO)
        lab.run(Watching(lab.box, alive={12}))
    else:
        lab.task(12, stage=Stage.FAILED, slot=NO_SLOT, updated_at=main._now())
        assert running_services(lab, 12)
        lab.run()

    assert running_services(lab, 12) == set()
    assert lab.ref(12) in lab.box.plans            # data is retained until cleanup


# --- S45 and the shared allocation view ------------------------------------

def test_s45_verification_takes_a_slot_but_no_unfinished_place(tmp_path):
    c = world(tmp_path, [project(tmp_path, "analytics")], capacity=2)
    unfinished(c, "analytics", 9)
    [live] = unfinished(c, "analytics", 1, live=True)
    gh = Gh({"analytics": issues(100, 1)})
    verifier = Verifier(["op-1"])
    sess = FakeSessions(alive={live.issue})
    d = deps(gh, sess)
    d.verification = verifier

    main.run_pass(c, d)

    assert [op for op, _ in verifier.started] == ["op-1"]
    assert open_total(c) == 10
    held = state.allocations(c.state_dir)
    assert sorted(a.owner_kind for a in held) == ["task", "verification"]
    assert len({a.slot for a in held}) == 2
    assert gh.claimed == [] and sess.spawned == []


def _admitted_verification(tmp_path, capacity):
    c = world(tmp_path, [project(tmp_path, "analytics")], capacity=capacity)
    unfinished(c, "analytics", 1, live=True)
    verifier = Verifier(["op-1"])
    gh = Gh({})
    pass_(c, gh, verifier)
    return c, gh, verifier


def pass_(c, gh, verifier, sess=None):
    sess = sess or FakeSessions()
    sess.alive_set = {t.issue for t in live_slots(c)}
    d = deps(gh, sess)
    d.verification = verifier
    main.run_pass(c, d)
    return sess


def test_no_task_can_take_a_third_slot_beside_a_verification(tmp_path):
    c, gh, verifier = _admitted_verification(tmp_path, capacity=2)
    assert [op for op, _ in verifier.started] == ["op-1"]
    gh.cands = {"analytics": issues(100, 2)}

    sess = pass_(c, gh, verifier)

    assert gh.claimed == [] and sess.spawned == []
    assert open_total(c) == 1


def test_no_triage_sweep_can_take_a_third_slot_beside_a_verification(tmp_path, monkeypatch):
    c, gh, verifier = _admitted_verification(tmp_path, capacity=2)
    assert [op for op, _ in verifier.started] == ["op-1"]
    launches = []
    monkeypatch.setattr(herdr.Tab, "ensure", staticmethod(
        lambda *a, **k: launches.append(a) or None))
    triage.enqueue(c.state_dir)

    pass_(c, gh, verifier)

    assert launches == [] and triage.pending(c.state_dir)


# --- crash / restart -------------------------------------------------------

def test_restart_keeps_a_running_verification_slot_and_never_double_allocates(tmp_path):
    c = world(tmp_path, [project(tmp_path, "analytics")], capacity=3)
    [live] = unfinished(c, "analytics", 1, live=True)
    gh = Gh({})
    first = Verifier(["op-1"])
    pass_(c, gh, first)
    [(_, slot)] = first.started
    assert slot != live.slot

    # the dispatcher restarts; the verification process is still running
    gh.cands = {"analytics": issues(99, 1)}
    after_restart = Verifier(running=["op-1"])
    pass_(c, gh, after_restart)

    assert gh.claimed == [99] and after_restart.started == []
    new = state.load(c.state_dir, "analytics", 99, repo="example/analytics-0")
    assert new.slot not in {live.slot, slot}
    held = state.allocations(c.state_dir)
    assert {(a.owner_kind, a.slot) for a in held if a.owner_kind == "verification"} == \
        {("verification", slot)}
    assert len({a.slot for a in held}) == len(held) == 3

    # once its process is gone the slot comes back
    gh.cands = {"analytics": issues(98, 1)}
    pass_(c, gh, Verifier())

    assert gh.claimed == [99, 98]
    assert "verification" not in {a.owner_kind for a in state.allocations(c.state_dir)}


# --- interrupted transitions ------------------------------------------------

class Crash(BaseException):
    """The dispatcher dies here."""


class CrashingBox(Box):
    """Dies right after starting the named service."""

    def __init__(self):
        super().__init__()
        self.crash_after = set()

    def start(self, plan, name):
        super().start(plan, name)
        if self.specs[name][1].service in self.crash_after:
            raise Crash(name)


def test_restart_stops_services_left_running_by_an_interrupted_resume(tmp_path, monkeypatch):
    lab = Lab(tmp_path, box=CrashingBox())
    wt = lab.task(12)
    park_intent(lab, 12)
    lab.run(Watching(lab.box, alive={12}))
    assert running_services(lab, 12) == set()

    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    state.save(lab.cfg.state_dir, replace(lab.load(12), park=PARK_WAKE))
    lab.box.crash_after = {"postgres", "redis"}
    with pytest.raises(Crash):
        lab.run(Watching(lab.box))
    assert running_services(lab, 12)

    # the dispatcher restarts; the woken task may not run yet
    lab.box.crash_after = set()
    patch_usage(monkeypatch, util=0.99)
    lab.run(Watching(lab.box))

    assert running_services(lab, 12) == set()


def test_verification_that_fails_after_launching_keeps_its_slot_while_running(tmp_path):
    c = world(tmp_path, [project(tmp_path, "analytics")], capacity=1)
    gh = Gh({"analytics": issues(99, 1)})

    class LostResponse(Verifier):
        def start(self, operation_id, slot):
            super().start(operation_id, slot)
            raise OSError("launch response lost")

    verifier = LostResponse(["op-1"])
    pass_(c, gh, verifier)

    assert "op-1" in verifier.running
    assert gh.claimed == []                       # its process still holds the only slot
    assert [(a.owner_kind, a.owner_id) for a in state.allocations(c.state_dir)] == \
        [("verification", "op-1")]

    verifier.running.clear()                      # the process is gone
    pass_(c, gh, verifier)

    assert gh.claimed == [99]
