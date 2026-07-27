from dispatcher import queue_ops
from dispatcher.queue_ops import NEXT_BOOST, QueuePlan


def row(number=7, title="t", status="Ready", labels=("auto",), blocked=False,
        score=1.0, boost=0):
    return {"number": number, "title": title, "url": f"u{number}",
            "status": status, "labels": list(labels), "blocked": blocked,
            "score": score, "boost": boost}


class FakeGitHub:
    def __init__(self):
        self.boosts, self.statused, self.labeled = [], [], []
        self.calls = []   # global order across the three mutators

    def set_boost(self, target, issue, value):
        self.boosts.append((issue, value))
        self.calls.append("set_boost")

    def set_status(self, target, issue, option_id):
        self.statused.append((issue, option_id))
        self.calls.append("set_status")

    def add_label(self, target, issue, label):
        self.labeled.append((issue, label))
        self.calls.append("add_label")


class FakeTarget:
    status_ready_option_id = "R"


# -- plan_boost ---------------------------------------------------------

def test_plan_boost_adds_amount_no_clamp():
    plan = queue_ops.plan_boost(row(boost=2), -3)
    assert plan == QueuePlan(ok=True, reason="#7 boost 2 → -1", set_boost=-1)


def test_plan_boost_defaults_missing_boost_to_zero():
    r = row()
    del r["boost"]
    plan = queue_ops.plan_boost(r, 5)
    assert plan.ok and plan.set_boost == 5 and plan.reason == "#7 boost 0 → 5"


def test_plan_boost_extreme_values_pass_through():
    # No band, no clamp — extraction must be faithful to main.py.
    assert queue_ops.plan_boost(row(boost=95), 10).set_boost == 105
    assert queue_ops.plan_boost(row(boost=-95), -10).set_boost == -105


# -- plan_next ----------------------------------------------------------

def test_plan_next_blocked_never_forceable():
    plan = queue_ops.plan_next(row(blocked=True), force=True)
    assert not plan.ok
    assert plan.reason == ("#7 is blocked — resolve its blockers first "
                           "(blocked issues cannot be forced)")
    assert plan.set_boost is None and not plan.set_ready and not plan.add_auto


def test_plan_next_in_progress_never_forceable():
    plan = queue_ops.plan_next(row(status="In progress"), force=True)
    assert not plan.ok
    assert plan.reason == ("#7 is already In progress — work on it is already "
                           "in flight (in-progress issues cannot be forced)")


def test_plan_next_eligible_sets_head_boost_only():
    plan = queue_ops.plan_next(row(), force=False)
    assert plan.ok and plan.set_boost == NEXT_BOOST
    assert not plan.set_ready and not plan.add_auto
    assert plan.reason == f"#7 enqueued at the head (boost {NEXT_BOOST})"


def test_plan_next_ineligible_without_force_lists_problems_and_hint():
    plan = queue_ops.plan_next(row(status="Backlog", labels=()), force=False)
    assert not plan.ok
    assert plan.reason == (
        "#7 is not eligible: status is Backlog, not Ready; "
        "missing the auto label.\n"
        "Send /next 7 force to make it eligible and enqueue.")


def test_plan_next_unset_status_reports_unset():
    plan = queue_ops.plan_next(row(status=None, labels=()), force=False)
    assert "status is unset, not Ready" in plan.reason


def test_plan_next_force_flips_status_and_label():
    plan = queue_ops.plan_next(row(status="Backlog", labels=()), force=True)
    assert plan.ok and plan.set_boost == NEXT_BOOST
    assert plan.set_ready and plan.add_auto


def test_plan_next_force_only_fixes_whats_missing():
    plan = queue_ops.plan_next(row(status="Ready", labels=()), force=True)
    assert plan.ok and not plan.set_ready and plan.add_auto


# -- plan_ready ---------------------------------------------------------

def test_plan_ready_rejects_in_progress():
    plan = queue_ops.plan_ready(row(status="In progress"))
    assert not plan.ok and "In progress" in plan.reason


def test_plan_ready_rejects_blocked():
    plan = queue_ops.plan_ready(row(blocked=True))
    assert not plan.ok and "blocked" in plan.reason


def test_plan_ready_noop_when_already_ready():
    plan = queue_ops.plan_ready(row(status="Ready"))
    assert not plan.ok and plan.reason == "#7 is already Ready"
    assert not plan.set_ready


def test_plan_ready_marks_ready():
    plan = queue_ops.plan_ready(row(status="Backlog"))
    assert plan.ok and plan.set_ready
    assert plan.set_boost is None and not plan.add_auto
    assert plan.reason == "#7 marked Ready"


# -- apply_plan ---------------------------------------------------------

def test_apply_plan_executes_boost_then_ready_then_label():
    gh = FakeGitHub()
    plan = QueuePlan(ok=True, reason="", set_boost=99, set_ready=True,
                     add_auto=True)
    queue_ops.apply_plan(gh, FakeTarget(), 7, plan)
    assert gh.calls == ["set_boost", "set_status", "add_label"]
    assert gh.boosts == [(7, 99)]
    assert gh.statused == [(7, "R")]
    assert gh.labeled == [(7, "auto")]


def test_apply_plan_skips_unset_actions():
    gh = FakeGitHub()
    queue_ops.apply_plan(gh, FakeTarget(), 7,
                         QueuePlan(ok=True, reason="", set_boost=-1))
    assert gh.calls == ["set_boost"] and gh.boosts == [(7, -1)]


def test_apply_plan_rejected_plan_is_a_noop():
    gh = FakeGitHub()
    queue_ops.apply_plan(gh, FakeTarget(), 7,
                         QueuePlan(ok=False, reason="nope", set_boost=99,
                                   set_ready=True, add_auto=True))
    assert gh.calls == []
