"""Task decisions through main.run_pass, real Sessions and real listener."""

import pytest

from dispatcher.state import AnswersRequest, Stage
from t5_dispatcher_support import DispatcherCase
from t5_runtime_support import command, completion, covers, finished, qualified, stored


@pytest.fixture(autouse=True)
def criterion_property(request, record_property):
    record_property("t5_criteria", ",".join(getattr(request.function, "t5_criteria", ())))


@covers("W01", "B01", "B06")
@pytest.mark.parametrize("elapsed,parked", [(660, False), (10800, False), (10801, True)],
                         ids=["ordinary-stall-exceeded", "exact-cap", "strictly-past-cap"])
def test_task_background_wait_holds_capacity_until_strict_cap(monkeypatch, elapsed, parked):
    with DispatcherCase(monkeypatch) as case:
        running = qualified(case.runtime, now=20000 - elapsed)[0]
        assert stored(case.runtime.view(), running["identity"])["eligible"]
        task = case.run()
        if parked:
            assert task.park and "180" in task.park_note
            assert case.physical_ends() and all(snapshot["retired"] for snapshot in case.physical_ends())
        else:
            assert task.stage == Stage.REVIEW and task.park == ""
            assert case.tab.alive and not case.physical_ends()
            assert not any(effect[0] in {"claim", "release"} for effect in case.github.effects)


@covers("U02", "U08", "B02", "B03", "B04")
@pytest.mark.parametrize("hold", ["active-input", "unknown-inventory", "pending-failure", "control-reconnect"])
def test_task_cap_respects_active_input_unknown_control_and_pending_failure(monkeypatch, hold):
    with DispatcherCase(monkeypatch) as case:
        running = qualified(case.runtime, now=1)[0]
        if hold == "active-input":
            case.runtime.start("operator-turn", now=200)
        elif hold == "unknown-inventory":
            case.runtime.inventory([running], certainty="unknown", now=200)
        elif hold == "pending-failure":
            case.runtime.inventory([finished(running, status="failed", exit_code=7, output="FAILURE_SUFFIX")], now=200)
            assert completion(case.runtime.view(), running["identity"]) is not None
        else:
            case.runtime.send({"type": "control/unknown", "message": "fixture control socket disconnected"}, now=200)
        assert stored(case.runtime.view(), running["identity"])["eligible"]
        task = case.run()
        assert task.stage == Stage.REVIEW and task.park == ""
        assert case.tab.alive and not case.physical_ends()
        assert case.runtime.view()["wait"]["since"] == 1


@covers("B07", "I07")
@pytest.mark.parametrize("signal", ["blocked", "awaiting-answers", "awaiting-ci", "awaiting-review", "done", "loop-cap"])
def test_existing_stage_authority_wins_over_running_background_work(monkeypatch, signal):
    stage = Stage.AWAITING_SPEC_REVIEW if signal == "awaiting-review" else Stage.REVIEW
    with DispatcherCase(monkeypatch, stage=stage) as case:
        running = qualified(case.runtime, now=100)[0]
        assert stored(case.runtime.view(), running["identity"])["eligible"]
        if signal == "awaiting-answers":
            (case.worktree / ".agent" / "answers.md").write_text("generic fixture questions\n")
            case.signal(signal, artifact=".agent/answers.md", note="fixture answers")
        elif signal == "awaiting-ci":
            case.signal(signal, run_id=123)
        elif signal == "awaiting-review":
            case.signal(signal, stage="spec")
        elif signal == "done":
            case.signal(signal, artifact="https://github.com/fixture/repo/pull/123")
        elif signal == "loop-cap":
            case.signal("working", loop="gate", round=3)
        else:
            case.signal(signal, note="fixture help")
        task = case.run()
        if signal == "done":
            assert task.stage == Stage.PR_OPEN
        else:
            assert task.park and case.physical_ends()
            assert all(snapshot["retired"] for snapshot in case.physical_ends())
            assert not case.runtime.send({"type": "inventory", "certainty": "known", "workers": [finished(running)]})
            if signal == "awaiting-ci":
                assert task.ci_run_id == 123
            elif signal == "awaiting-answers":
                assert task.operator_request == AnswersRequest(path=".agent/answers.md")
            elif signal == "loop-cap":
                assert task.stage == Stage.REVIEW and task.gate_rounds == 3
                assert task.park == "parked" and task.slot == -1
                assert task.park_note == "gate loop exceeded its cap of 2 rounds"


@covers("B08")
def test_dead_service_and_exited_physical_session_keep_failed_resume_behavior(monkeypatch):
    with DispatcherCase(monkeypatch) as case:
        running = qualified(case.runtime)[0]
        assert stored(case.runtime.view(), running["identity"])["eligible"]
        case.runtime.send({"type": "service", "status": "dead"}, now=200)
        case.tab.alive = False
        task = case.run()
        assert task.stage == Stage.FAILED and task.crashed_stage == "review"
        assert case.runtime.view()["retired"], "physical crash closure must leave exact launch fenced"


@covers("B05")
def test_task_normal_empty_work_stop_still_parks_and_fences_before_physical_end(monkeypatch):
    with DispatcherCase(monkeypatch) as case:
        case.runtime.stop()
        assert case.runtime.send({"type": "inventory", "certainty": "known", "workers": []})
        task = case.run()
        assert task.park and "waiting for input" in task.park_note
        assert case.physical_ends() and all(snapshot["retired"] for snapshot in case.physical_ends())
