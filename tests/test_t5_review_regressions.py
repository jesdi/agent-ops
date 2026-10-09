"""Review regressions through the public listener and native process boundary."""
from copy import deepcopy

import pytest

from t5_observed_session import ObservedSession
from t5_runtime_support import RuntimeCase, agent, qualified


@pytest.mark.parametrize("status", [{}, {"type": "unsupported-required-status"},
    {"type": "active"}, {"type": "active", "activeFlags": "waitingOnApproval"},
    {"type": "active", "activeFlags": ["unsupported-flag"]}])
def test_unreadable_owned_child_status_cannot_supply_empty_inventory(monkeypatch, status):
    with ObservedSession(monkeypatch) as session:
        session.backend("hold_next", name="discover", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "discover"})
        session.backend("create_thread", thread_id="unreadable-status", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="unreadable-status", turn_id="actual-running")
        reduced = deepcopy(session.backend("snapshot")["threads"]["unreadable-status"])
        reduced.update(status=status, turns=reduced["turns"] if status.get("type") == "active" else [])
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "unreadable-status"},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        session.backend("release", name="discover")
        assert session.until(lambda: len([r for r in session.rows() if r["kind"] == "rpc_received"
            and r["method"] == "thread/read" and r["params"].get("threadId") == "unreadable-status"]) >= 2)
        session.backend("hold_next", name="inspect", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect"})
        snapshot = session.view()
        assert snapshot["inventory"] == "unknown"
        assert session.client.retire(session.binding, snapshot["revision"], reason="stopped") == "held"
        assert snapshot["completions"] == []
        session.backend("release", name="inspect")


@pytest.mark.parametrize("older_turn", [None, "completed", "failed", "interrupted"],
                         ids=["empty-turns", "older-completed-turn", "older-failed-turn", "older-interrupted-turn"])
def test_active_owned_child_missing_current_turn_holds_until_history_recovers(monkeypatch, older_turn):
    with ObservedSession(monkeypatch) as session:
        session.backend("hold_next", name="before-child", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "before-child"})
        session.backend("create_thread", thread_id="hidden-active-child", parent=session.root, depth=1)
        if older_turn:
            session.backend("start_turn", thread_id="hidden-active-child", turn_id="old-child-turn")
            session.backend("end_turn", thread_id="hidden-active-child", turn_id="old-child-turn",
                            status=older_turn, error={"message": "earlier child failure"} if older_turn == "failed" else None,
                            items=[{"type": "agentMessage", "id": "child-result", "text": "partial result before failure"}])
        session.backend("start_turn", thread_id="hidden-active-child", turn_id="current-child-turn",
                        active_flags=["waitingOnUserInput"])
        native = session.backend("snapshot")["threads"]["hidden-active-child"]
        reduced = deepcopy(native)
        reduced["turns"] = reduced["turns"][:-1]
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "hidden-active-child"},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "hidden-active-child"},
                        fault="error", times=1000)
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        session.backend("release", name="before-child")
        assert session.until(lambda: len([r for r in session.rows() if r["kind"] == "rpc_received"
            and r["method"] == "thread/read" and r["params"].get("threadId") == "hidden-active-child"]) >= 2)
        # Freeze the next scan after previous reports reached the real listener.
        session.backend("hold_next", name="inspect-inventory", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-inventory"})
        snapshot = session.view()
        assert snapshot["inventory"] == "unknown"
        assert session.client.retire(session.binding, snapshot["revision"], reason="stopped") == "held"
        assert not snapshot["retired"]
        assert not any(w["identity"].get("turn_id") == "current-child-turn" for w in snapshot["workers"])
        if older_turn == "failed":
            failure = next(c for c in snapshot["completions"] if c["identity"].get("turn_id") == "old-child-turn")
            assert failure["outcome"]["error"]["message"] == "earlier child failure"
            assert failure["outcome"]["messages"] == [
                {"item_id": "child-result", "text": "partial result before failure"}]
        if older_turn in ("completed", "interrupted"):
            completion = next((c for c in snapshot["completions"] if c["identity"].get("turn_id") == "old-child-turn"), None)
            assert completion is not None
            assert completion["outcome"] == dict(status=older_turn, error=None, messages=[
                {"item_id": "child-result", "text": "partial result before failure"}])
        if older_turn:
            stored = next(w for w in snapshot["workers"] if w["identity"].get("turn_id") == "old-child-turn")
            assert stored["thread_status"] == "active"
            assert stored["active_flags"] == ["waitingOnUserInput"]
        checkpoint = deepcopy(snapshot["history_checkpoint"])
        session.backend("clear_faults")
        session.backend("release", name="inspect-inventory")
        assert session.until(lambda: session.view()["inventory"] == "known" and any(
            w["identity"].get("turn_id") == "current-child-turn" and w["status"] == "running"
            for w in session.view()["workers"]))
        assert session.view()["history_checkpoint"]["baseline_turns"] == checkpoint["baseline_turns"]
        assert session.view()["history_checkpoint"]["baseline_workers"] == checkpoint["baseline_workers"]


@pytest.mark.parametrize("partial", ["omitted-worker", "malformed-ownership"])
def test_incomplete_reconciliation_cannot_report_new_work_against_stopped_clock(tmp_path, partial):
    case = RuntimeCase(tmp_path)
    original = qualified(case, now=100)[0]
    newcomer = agent(case)
    workers = [newcomer]
    if partial == "malformed-ownership":
        malformed = agent(case, owner="invalid-child")
        malformed["ancestry"][0]["source_kind"] = "other"
        workers.extend([original, malformed])
    case.inventory(workers, now=300)
    snapshot = case.view()
    assert snapshot["inventory"] == "unknown"
    assert snapshot["wait"]["since"] == 100
    assert snapshot["wait"]["ever_reported"] == [original["identity"]]
    case.inventory([original, newcomer], now=400)
    snapshot = case.view()
    assert snapshot["inventory"] == "known"
    assert snapshot["wait"]["since"] == 400
    assert newcomer["identity"] in snapshot["wait"]["ever_reported"]


@pytest.mark.parametrize("owning_turn_hidden", [False, True])
@pytest.mark.parametrize("root_owner", [False, True])
def test_complete_older_command_failure_survives_unreadable_new_active_turn(monkeypatch, owning_turn_hidden, root_owner):
    from t5_executable_support import worker, result
    with ObservedSession(monkeypatch) as session:
        owner = session.root if root_owner else "command-owner"
        turn = session.initial_turn if root_owner else "command-turn"
        if not root_owner:
            session.backend("create_thread", thread_id=owner, parent=session.root, depth=1)
            session.backend("start_turn", thread_id=owner, turn_id=turn)
        session.backend("start_command", thread_id=owner, turn_id=turn, item_id="offline-failure")
        session.backend("end_turn", thread_id=owner, turn_id=turn)
        assert session.until(lambda: (worker(session.view(), "offline-failure") or {}).get("eligible"))
        stops = deepcopy(session.view()["main"]["completed_turns"])
        session.backend("hold_next", name="reconnect", phase="before", matches={"method": "thread/resume", "threadId": session.root})
        peers = session.backend("snapshot")["peers"]
        controller = next(p for p in peers if p["client_name"] != "fixture-terminal" and session.root in p["subscriptions"])
        session.backend("disconnect", peer=controller["id"])
        session.backend("wait", matches={"kind": "request_held", "name": "reconnect"})
        session.backend("end_command", thread_id=owner, item_id="offline-failure",
                        status="failed", exit_code=7, output="older turn final output")
        if root_owner:
            session.terminal("rpc", method="turn/start", params={"threadId": owner,
                "clientUserMessageId": "next-input", "input": [{"type": "text", "text": "operator input"}]})
        else:
            session.backend("start_turn", thread_id=owner, turn_id="hidden-new-turn")
        reduced = deepcopy(session.backend("snapshot")["threads"][owner])
        reduced["turns"] = [] if owning_turn_hidden else reduced["turns"][:-1]
        session.backend("fault_next", matches={"method": "thread/read", "threadId": owner},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": owner},
                        fault="error", times=1000)
        session.backend("release", name="reconnect")
        assert session.until(lambda: result(session.view(), "offline-failure") is not None)
        snapshot = session.view()
        assert result(snapshot, "offline-failure")["outcome"]["aggregated_output"] == "older turn final output"
        assert result(snapshot, "offline-failure")["outcome"]["exit_code"] == 7
        assert snapshot["inventory"] == "unknown"
        assert snapshot["main"]["status"] == "active"
        assert not snapshot["retired"]
        assert worker(snapshot, "offline-failure")["turn_id"] == turn
        assert snapshot["main"]["completed_turns"] == stops


@pytest.mark.parametrize("turn_history_readable", [False, True], ids=["unavailable-history", "complete-turn-history"])
def test_system_error_child_with_hidden_failed_turn_holds_until_outcome_recovers(monkeypatch, turn_history_readable):
    with ObservedSession(monkeypatch) as session:
        session.backend("hold_next", name="discover-failure", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "discover-failure"})
        baseline = deepcopy(session.view()["history_checkpoint"])
        session.backend("create_thread", thread_id="hidden-failed-child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="hidden-failed-child", turn_id="child-failure")
        session.backend("end_turn", thread_id="hidden-failed-child", turn_id="child-failure",
                        status="failed", error={"message": "POST_SEED_CHILD_FAILURE", "codexErrorInfo": "other"})
        actual = session.backend("snapshot")["threads"]["hidden-failed-child"]
        assert actual["status"] == {"type": "systemError"}
        reduced = deepcopy(actual)
        reduced["turns"] = []
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "hidden-failed-child"},
                        fault={"result": {"thread": reduced}}, times=1000)
        if turn_history_readable:
            session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "hidden-failed-child"},
                            fault={"result": {"data": actual["turns"], "nextCursor": None}}, times=1000)
        else:
            session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "hidden-failed-child"},
                            fault="error", times=1000)
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        session.backend("release", name="discover-failure")
        assert session.until(lambda: len([r for r in session.rows() if r["kind"] == "rpc_received"
            and r["method"] == "thread/read" and r["params"].get("threadId") == "hidden-failed-child"]) >= 2)
        # A later discovery fence lets both preceding inventory reports settle.
        session.backend("hold_next", name="inspect-failure", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-failure"})
        snapshot = session.view()
        identity = {"kind": "agent", "thread_id": "hidden-failed-child", "turn_id": "child-failure"}
        expected = {"identity": identity, "outcome": {"status": "failed", "messages": [],
                    "error": {"message": "POST_SEED_CHILD_FAILURE", "codex_error_info": "other"}}}
        assert snapshot["inventory"] == ("known" if turn_history_readable else "unknown")
        assert snapshot["completions"] == ([expected] if turn_history_readable else [])
        assert session.client.retire(session.binding, snapshot["revision"], reason="stopped") == "held"
        assert snapshot["main"]["status"] == "stopped"
        stops = deepcopy(snapshot["main"]["completed_turns"])
        assert list(stops) == [session.initial_turn]
        assert snapshot["wait"] is None
        session.backend("clear_faults")
        session.backend("release", name="inspect-failure")
        assert session.until(lambda: session.view()["inventory"] == "known" and session.view()["completions"] == [expected])
        recovered = session.view()
        child = next(w for w in recovered["workers"] if w["identity"] == identity)
        assert child["thread_status"] == "systemError"
        assert child["active_flags"] == []
        assert recovered["main"]["completed_turns"] == stops
        assert recovered["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert recovered["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]


@pytest.mark.parametrize("active_flags", [["waitingOnUserInput"], ["waitingOnApproval", "waitingOnUserInput"]])
def test_failure_before_first_child_history_retains_validated_discovery_status(monkeypatch, active_flags):
    with ObservedSession(monkeypatch) as session:
        session.backend("hold_next", name="discover-before-read", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "discover-before-read"})
        baseline = deepcopy(session.view()["history_checkpoint"])
        session.backend("create_thread", thread_id="first-unreadable-child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="first-unreadable-child", turn_id="failed-before-read", active_flags=active_flags)
        session.backend("hold_next", name="first-child-read", phase="before",
                        matches={"method": "thread/read", "threadId": "first-unreadable-child"})
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "first-unreadable-child"},
                        fault="error", times=1000)
        session.backend("release", name="discover-before-read")
        session.backend("wait", matches={"kind": "request_held", "name": "first-child-read"})
        session.backend("end_turn", thread_id="first-unreadable-child", turn_id="failed-before-read",
                        status="failed", error={"message": "KNOWN_CHILD_FAILURE"})
        session.backend("start_turn", thread_id="first-unreadable-child", turn_id="new-child-turn", active_flags=active_flags)
        session.backend("hold_next", name="inspect-first-read", phase="before", matches={"method": "thread/list"})
        session.backend("release", name="first-child-read")
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-first-read"})
        snapshot = session.view()
        identity = {"kind": "agent", "thread_id": "first-unreadable-child", "turn_id": "failed-before-read"}
        expected = {"identity": identity, "outcome": {"status": "failed", "messages": [],
                    "error": {"message": "KNOWN_CHILD_FAILURE", "codex_error_info": None}}}
        assert snapshot["completions"] == [expected]
        child = next(w for w in snapshot["workers"] if w["identity"] == identity)
        assert child["thread_status"] == "active"
        assert child["active_flags"] == active_flags
        assert snapshot["inventory"] == "unknown"
        assert snapshot["main"]["status"] == "active"
        assert snapshot["main"]["completed_turns"] == {}
        assert snapshot["wait"] is None
        assert session.client.retire(session.binding, snapshot["revision"], reason="stopped") == "held"
        session.backend("clear_faults")
        session.backend("release", name="inspect-first-read")
        assert session.until(lambda: session.view()["inventory"] == "known" and any(
            w["identity"].get("turn_id") == "new-child-turn" and w["status"] == "running"
            for w in session.view()["workers"]))
        recovered = session.view()
        assert recovered["completions"] == [expected]
        assert recovered["main"]["completed_turns"] == {}
        assert recovered["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert recovered["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]


@pytest.mark.parametrize("definite_failure", [False, True], ids=["empty-unloaded-history", "known-older-failure"])
def test_child_still_not_loaded_after_resume_cannot_supply_empty_inventory(monkeypatch, definite_failure):
    with ObservedSession(monkeypatch) as session:
        session.backend("hold_next", name="discover-unloaded", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "discover-unloaded"})
        baseline = deepcopy(session.view()["history_checkpoint"])
        session.backend("create_thread", thread_id="still-unloaded-child", parent=session.root, depth=1)
        if definite_failure:
            session.backend("start_turn", thread_id="still-unloaded-child", turn_id="earlier-failed-turn")
            session.backend("end_turn", thread_id="still-unloaded-child", turn_id="earlier-failed-turn",
                            status="failed", error={"message": "AVAILABLE_BEFORE_LOADING"})
        session.backend("start_turn", thread_id="still-unloaded-child", turn_id="unreadable-running-turn")
        session.backend("set_thread", thread_id="still-unloaded-child", overrides={"status": {"type": "notLoaded"}})
        reduced = deepcopy(session.backend("snapshot")["threads"]["still-unloaded-child"])
        reduced["turns"] = reduced["turns"][:-1]
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "still-unloaded-child"},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/resume", "threadId": "still-unloaded-child"},
                        fault={"result": {"thread": reduced, "model": "fixture", "modelProvider": "fixture",
                            "cwd": str(session.worktree), "approvalPolicy": "never", "approvalsReviewer": "user",
                            "sandbox": {"type": "dangerFullAccess"}}}, times=1000)
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        session.backend("release", name="discover-unloaded")
        # Each attempted load has two exact reads; four reads require two scans.
        assert session.until(lambda: len([r for r in session.rows() if r["kind"] == "rpc_received"
            and r["method"] == "thread/read" and r["params"].get("threadId") == "still-unloaded-child"]) >= 4)
        session.backend("hold_next", name="inspect-unloaded", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-unloaded"})
        snapshot = session.view()
        assert snapshot["inventory"] == "unknown"
        assert snapshot["main"]["status"] == "stopped"
        stops = deepcopy(snapshot["main"]["completed_turns"])
        assert list(stops) == [session.initial_turn]
        assert snapshot["wait"] is None
        assert session.client.retire(session.binding, snapshot["revision"], reason="stopped") == "held"
        assert not snapshot["retired"]
        expected = [{"identity": {"kind": "agent", "thread_id": "still-unloaded-child", "turn_id": "earlier-failed-turn"},
                     "outcome": {"status": "failed", "messages": [],
                                 "error": {"message": "AVAILABLE_BEFORE_LOADING", "codex_error_info": None}}}] if definite_failure else []
        assert snapshot["completions"] == expected
        assert not any(w["identity"].get("turn_id") == "unreadable-running-turn" for w in snapshot["workers"])
        session.backend("clear_faults")
        session.backend("release", name="inspect-unloaded")
        assert session.until(lambda: session.view()["inventory"] == "known" and any(
            w["identity"].get("turn_id") == "unreadable-running-turn" and w["status"] == "running"
            for w in session.view()["workers"]))
        recovered = session.view()
        assert recovered["completions"] == expected
        assert recovered["main"]["completed_turns"] == stops
        assert recovered["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert recovered["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]


@pytest.mark.parametrize("other_turn_visible", [False, True], ids=["hidden-other-turn", "visible-other-turn"])
def test_another_turn_history_cannot_supply_original_command_result(monkeypatch, other_turn_visible):
    from t5_executable_support import worker, result
    with ObservedSession(monkeypatch) as session:
        session.backend("create_thread", thread_id="command-owner", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="command-owner", turn_id="original-command-turn")
        session.backend("start_command", thread_id="command-owner", turn_id="original-command-turn", item_id="original-item")
        session.backend("end_turn", thread_id="command-owner", turn_id="original-command-turn")
        assert session.until(lambda: (worker(session.view(), "original-item") or {}).get("eligible"))
        original = deepcopy(worker(session.view(), "original-item"))
        baseline = deepcopy(session.view()["history_checkpoint"])
        stops = deepcopy(session.view()["main"]["completed_turns"])
        session.backend("hold_next", name="identity-reconnect", phase="before",
                        matches={"method": "thread/resume", "threadId": session.root})
        controller = next(p for p in session.backend("snapshot")["peers"]
                          if p["client_name"] != "fixture-terminal" and session.root in p["subscriptions"])
        session.backend("disconnect", peer=controller["id"])
        session.backend("wait", matches={"kind": "request_held", "name": "identity-reconnect"})
        session.backend("end_command", thread_id="command-owner", item_id="original-item",
                        status="failed", exit_code=7, output="ORIGINAL_COMMAND_FAILURE")
        session.backend("start_turn", thread_id="command-owner", turn_id="different-command-turn")
        actual = session.backend("snapshot")["threads"]["command-owner"]
        reduced = deepcopy(actual)
        reduced["turns"] = reduced["turns"][-1:] if other_turn_visible else []
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "command-owner"},
                        fault={"result": {"thread": reduced}}, times=1000)
        unrelated = deepcopy(next(item for turn in actual["turns"] for item in turn["items"] if item["id"] == "original-item"))
        unrelated.update(status="completed", exitCode=0, aggregatedOutput="OTHER_TURN_SUCCESS")
        session.backend("fault_next", matches={"method": "thread/items/list", "threadId": "command-owner"},
                        fault={"result": {"data": [{"turnId": "different-command-turn", "item": unrelated}],
                                          "nextCursor": None}}, times=1000)
        reads_before = len([r for r in session.rows() if r["kind"] == "rpc_received"
            and r["method"] == "thread/read" and r["params"].get("threadId") == "command-owner"])
        session.backend("release", name="identity-reconnect")
        assert session.until(lambda: len([r for r in session.rows() if r["kind"] == "rpc_received"
            and r["method"] == "thread/read" and r["params"].get("threadId") == "command-owner"]) >= reads_before + 2)
        session.backend("hold_next", name="inspect-command-identity", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-command-identity"})
        snapshot = session.view()
        assert result(snapshot, "original-item") is None
        retained = worker(snapshot, "original-item")
        assert retained["turn_id"] == original["turn_id"]
        assert retained["qualifying_stop"] == original["qualifying_stop"]
        assert retained["outcome"] is None
        assert snapshot["inventory"] == "unknown"
        assert any(alert["kind"] == "compatibility" for alert in snapshot["alerts"])
        assert snapshot["main"]["status"] == "active"
        assert snapshot["main"]["completed_turns"] == stops
        assert snapshot["wait"] is None
        session.backend("clear_faults")
        session.backend("release", name="inspect-command-identity")
        assert session.until(lambda: result(session.view(), "original-item") is not None)
        recovered = session.view()
        assert result(recovered, "original-item") == {"identity": original["identity"], "outcome": {
            "status": "failed", "exit_code": 7, "aggregated_output": "ORIGINAL_COMMAND_FAILURE", "duration_ms": None}}
        assert worker(recovered, "original-item")["turn_id"] == original["turn_id"]
        assert worker(recovered, "original-item")["qualifying_stop"] == original["qualifying_stop"]
        assert recovered["main"]["completed_turns"] == stops
        assert recovered["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert recovered["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]


def test_another_turn_notification_cannot_supply_original_command_result(monkeypatch):
    from t5_executable_support import worker, result
    with ObservedSession(monkeypatch) as session:
        session.backend("create_thread", thread_id="notification-owner", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="notification-owner", turn_id="notification-command-turn")
        command = session.backend("start_command", thread_id="notification-owner", turn_id="notification-command-turn",
                                  item_id="notification-item")
        session.backend("end_turn", thread_id="notification-owner", turn_id="notification-command-turn")
        assert session.until(lambda: (worker(session.view(), "notification-item") or {}).get("eligible"))
        session.backend("start_turn", thread_id="notification-owner", turn_id="different-notification-turn")
        session.backend("hold_next", name="inspect-notification", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-notification"})
        before = session.view()
        original = deepcopy(worker(before, "notification-item"))
        unrelated = deepcopy(command)
        unrelated.update(status="completed", exitCode=0, aggregatedOutput="OTHER_NOTIFICATION_SUCCESS")
        session.backend("notification", method="item/completed", params={"threadId": "notification-owner",
                        "turnId": "different-notification-turn", "item": unrelated})
        assert session.until(lambda: session.view()["revision"] > before["revision"])
        snapshot = session.view()
        assert any(alert["kind"] == "compatibility" and alert not in before["alerts"] for alert in snapshot["alerts"])
        assert snapshot["inventory"] == "unknown"
        assert result(snapshot, "notification-item") is None
        assert worker(snapshot, "notification-item")["turn_id"] == original["turn_id"]
        assert worker(snapshot, "notification-item")["qualifying_stop"] == original["qualifying_stop"]
        assert snapshot["main"]["completed_turns"] == before["main"]["completed_turns"]
        assert snapshot["wait"] == before["wait"]
        session.backend("end_command", thread_id="notification-owner", item_id="notification-item",
                        status="failed", exit_code=7, output="ACTUAL_NOTIFICATION_FAILURE")
        session.backend("release", name="inspect-notification")
        assert session.until(lambda: result(session.view(), "notification-item") is not None)
        recovered = session.view()
        assert result(recovered, "notification-item") == {"identity": original["identity"], "outcome": {
            "status": "failed", "exit_code": 7, "aggregated_output": "ACTUAL_NOTIFICATION_FAILURE", "duration_ms": None}}
        assert worker(recovered, "notification-item")["turn_id"] == original["turn_id"]
        assert worker(recovered, "notification-item")["qualifying_stop"] == original["qualifying_stop"]
        assert recovered["main"]["completed_turns"] == before["main"]["completed_turns"]


def test_baseline_failed_child_cannot_hide_new_failure_in_same_status_history(monkeypatch):
    script = [
        dict(action="create_thread", thread_id="root-fixture"),
        dict(action="start_turn", thread_id="root-fixture", turn_id="baseline-root"),
        dict(action="end_turn", thread_id="root-fixture", turn_id="baseline-root"),
        dict(action="create_thread", thread_id="repeated-failure-child", parent="root-fixture", depth=1),
        dict(action="start_turn", thread_id="repeated-failure-child", turn_id="baseline-failure"),
        dict(action="end_turn", thread_id="repeated-failure-child", turn_id="baseline-failure",
             status="failed", error={"message": "OLD_BASELINE_FAILURE"}),
        dict(action="hold_next", name="bootstrap", phase="before", matches={"method": "turn/start"}),
    ]
    with ObservedSession(monkeypatch, script=script, resume="root-fixture", bootstrap_pending=True) as session:
        session.backend("wait", matches={"kind": "request_held", "name": "bootstrap"})
        baseline = deepcopy(session.view()["history_checkpoint"])
        assert {"thread_id": "repeated-failure-child", "turn_id": "baseline-failure"} in baseline["baseline_turns"]
        session.backend("release", name="bootstrap")
        session.finish_start()
        session.backend("hold_next", name="same-status-reconnect", phase="before",
                        matches={"method": "thread/resume", "threadId": session.root})
        peer = next(p for p in session.backend("snapshot")["peers"]
                    if p["client_name"] != "fixture-terminal" and session.root in p["subscriptions"])
        session.backend("disconnect", peer=peer["id"])
        session.backend("wait", matches={"kind": "request_held", "name": "same-status-reconnect"})
        session.backend("start_turn", thread_id="repeated-failure-child", turn_id="latest-failure")
        session.backend("end_turn", thread_id="repeated-failure-child", turn_id="latest-failure",
                        status="failed", error={"message": "NEW_POST_SEED_FAILURE", "codexErrorInfo": "other"})
        actual = session.backend("snapshot")["threads"]["repeated-failure-child"]
        reduced = deepcopy(actual)
        reduced["turns"] = reduced["turns"][:1]
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "repeated-failure-child"},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "repeated-failure-child"},
                        fault="error", times=1000)
        reads_before = sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == "repeated-failure-child" for r in session.rows())
        session.backend("release", name="same-status-reconnect")
        assert session.until(lambda: sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == "repeated-failure-child" for r in session.rows()) >= reads_before + 2)
        session.backend("hold_next", name="before-root-stop", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "before-root-stop"})
        active = session.view()
        assert active["main"]["status"] == "active"
        assert active["inventory"] == "unknown"
        assert active["completions"] == []
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        reads_before = sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == "repeated-failure-child" for r in session.rows())
        session.backend("release", name="before-root-stop")
        assert session.until(lambda: sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == "repeated-failure-child" for r in session.rows()) >= reads_before + 2)
        session.backend("hold_next", name="inspect-hidden-latest", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-hidden-latest"})
        hidden = session.view()
        assert hidden["inventory"] == "unknown"
        assert hidden["main"]["status"] == "stopped"
        assert session.client.retire(session.binding, hidden["revision"], reason="stopped") == "held"
        assert hidden["completions"] == []
        assert hidden["wait"] is None
        stops = deepcopy(hidden["main"]["completed_turns"])
        assert list(stops) == [session.initial_turn]
        session.backend("clear_faults")
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "repeated-failure-child"},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "repeated-failure-child", "cursor": None},
                        fault={"result": {"data": actual["turns"][:1], "nextCursor": "opaque-child-turn-page"}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "repeated-failure-child", "cursor": "opaque-child-turn-page"},
                        fault={"result": {"data": actual["turns"][1:], "nextCursor": None}}, times=1000)
        session.backend("release", name="inspect-hidden-latest")
        identity = {"kind": "agent", "thread_id": "repeated-failure-child", "turn_id": "latest-failure"}
        expected = {"identity": identity, "outcome": {"status": "failed", "messages": [],
                    "error": {"message": "NEW_POST_SEED_FAILURE", "codex_error_info": "other"}}}
        assert session.until(lambda: session.view()["inventory"] == "known" and session.view()["completions"] == [expected])
        recovered = session.view()
        child = next(w for w in recovered["workers"] if w["identity"] == identity)
        assert child["thread_status"] == "systemError"
        assert child["active_flags"] == []
        assert recovered["main"]["completed_turns"] == stops
        assert recovered["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert recovered["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]
        assert recovered["history_checkpoint"]["seen_completions"] == [identity]
        assert session.client.retire(session.binding, recovered["revision"], reason="stopped") == "held"


@pytest.mark.parametrize(("latest_status", "read_form"), [
    ("completed", "empty"), ("completed", "older"), ("interrupted", "older")])
def test_idle_child_requires_complete_history_for_itemless_outcomes(monkeypatch, latest_status, read_form):
    with ObservedSession(monkeypatch) as session:
        session.backend("hold_next", name="discover-idle-history", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "discover-idle-history"})
        baseline = deepcopy(session.view()["history_checkpoint"])
        session.backend("create_thread", thread_id="idle-history-child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="idle-history-child", turn_id="older-idle-turn")
        session.backend("end_turn", thread_id="idle-history-child", turn_id="older-idle-turn")
        session.backend("start_turn", thread_id="idle-history-child", turn_id="latest-idle-turn")
        session.backend("end_turn", thread_id="idle-history-child", turn_id="latest-idle-turn", status=latest_status)
        actual = session.backend("snapshot")["threads"]["idle-history-child"]
        assert actual["status"] == {"type": "idle"}
        reduced = deepcopy(actual)
        reduced["turns"] = [] if read_form == "empty" else reduced["turns"][:1]
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "idle-history-child"},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "idle-history-child"},
                        fault="error", times=1000)
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        session.backend("release", name="discover-idle-history")
        assert session.until(lambda: sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == "idle-history-child" for r in session.rows()) >= 2)
        session.backend("hold_next", name="inspect-idle-history", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-idle-history"})
        hidden = session.view()
        assert hidden["inventory"] == "unknown"
        assert session.client.retire(session.binding, hidden["revision"], reason="stopped") == "held"
        assert hidden["main"]["status"] == "stopped"
        assert hidden["wait"] is None
        older = {"identity": {"kind": "agent", "thread_id": "idle-history-child", "turn_id": "older-idle-turn"},
                 "outcome": {"status": "completed", "messages": [], "error": None}}
        latest = {"identity": {"kind": "agent", "thread_id": "idle-history-child", "turn_id": "latest-idle-turn"},
                  "outcome": {"status": latest_status, "messages": [], "error": None}}
        assert hidden["completions"] == ([] if read_form == "empty" else [older])
        stops = deepcopy(hidden["main"]["completed_turns"])
        assert list(stops) == [session.initial_turn]
        session.backend("clear_faults")
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "idle-history-child"},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "idle-history-child", "cursor": None},
                        fault={"result": {"data": actual["turns"][:1], "nextCursor": "opaque-idle-page"}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "threadId": "idle-history-child", "cursor": "opaque-idle-page"},
                        fault={"result": {"data": actual["turns"][1:], "nextCursor": None}}, times=1000)
        session.backend("release", name="inspect-idle-history")
        assert session.until(lambda: session.view()["inventory"] == "known" and len(session.view()["completions"]) == 2)
        recovered = session.view()
        assert sorted(recovered["completions"], key=lambda c: c["identity"]["turn_id"]) == [latest, older]
        assert all(w["thread_status"] == "idle" and w["active_flags"] == [] for w in recovered["workers"])
        assert recovered["main"]["completed_turns"] == stops
        assert recovered["wait"] is None
        assert recovered["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert recovered["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]
        assert session.client.retire(session.binding, recovered["revision"], reason="stopped") == "held"


@pytest.mark.parametrize("read_form", ["readable-older-message", "paged-itemless-outcome"])
@pytest.mark.parametrize("older_status", ["completed", "failed", "interrupted"])
def test_unloaded_child_preserves_readable_older_outcome_and_message(monkeypatch, older_status, read_form):
    with ObservedSession(monkeypatch) as session:
        session.backend("hold_next", name="discover-older-unloaded-result", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "discover-older-unloaded-result"})
        baseline = deepcopy(session.view()["history_checkpoint"])
        session.backend("create_thread", thread_id="unloaded-result-child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="unloaded-result-child", turn_id="readable-older-result")
        message = {"type": "agentMessage", "id": "older-result-message", "text": "AVAILABLE_DEFINITE_MESSAGE"}
        visible = read_form == "readable-older-message"
        error = {"message": "AVAILABLE_DEFINITE_ERROR", "codexErrorInfo": "other"} if older_status == "failed" else None
        session.backend("end_turn", thread_id="unloaded-result-child", turn_id="readable-older-result",
                        status=older_status, error=error, items=[message] if visible else [])
        session.backend("start_turn", thread_id="unloaded-result-child", turn_id="unavailable-current-turn",
                        active_flags=["waitingOnApproval"])
        session.backend("set_thread", thread_id="unloaded-result-child", overrides={"status": {"type": "notLoaded"}})
        reduced = deepcopy(session.backend("snapshot")["threads"]["unloaded-result-child"])
        reduced["turns"] = reduced["turns"][:1] if visible else []
        for method in ("thread/read", "thread/resume"):
            session.backend("fault_next", matches={"method": method, "threadId": "unloaded-result-child"},
                            fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/items/list", "threadId": "unloaded-result-child"},
                        fault={"result": {"data": [{"turnId": "readable-older-result", "item": message}] if visible else [],
                                          "nextCursor": None}}, times=1000)
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        session.backend("release", name="discover-older-unloaded-result")
        assert session.until(lambda: sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == "unloaded-result-child" for r in session.rows()) >= 4)
        session.backend("hold_next", name="inspect-older-unloaded-result", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-older-unloaded-result"})
        hidden = session.view()
        assert hidden["inventory"] == "unknown"
        assert session.client.retire(session.binding, hidden["revision"], reason="stopped") == "held"
        identity = {"kind": "agent", "thread_id": "unloaded-result-child", "turn_id": "readable-older-result"}
        outcome = {"status": older_status,
                   "messages": [{"item_id": "older-result-message", "text": "AVAILABLE_DEFINITE_MESSAGE"}] if visible else [],
                   "error": {"message": "AVAILABLE_DEFINITE_ERROR", "codex_error_info": "other"} if error else None}
        assert hidden["completions"] == [{"identity": identity, "outcome": outcome}]
        older = next(w for w in hidden["workers"] if w["identity"] == identity)
        assert older["thread_status"] == "notLoaded"
        assert older["active_flags"] == []
        assert len(older["ancestry"]) == 2
        assert older["ancestry"][-1]["thread_id"] == session.root
        assert not any(w["identity"].get("turn_id") == "unavailable-current-turn" for w in hidden["workers"])
        assert hidden["main"]["status"] == "stopped"
        stops = deepcopy(hidden["main"]["completed_turns"])
        assert list(stops) == [session.initial_turn]
        assert hidden["wait"] is None
        assert hidden["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert hidden["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]
        session.backend("clear_faults")
        session.backend("set_thread", thread_id="unloaded-result-child",
                        overrides={"status": {"type": "active", "activeFlags": ["waitingOnApproval"]}})
        session.backend("release", name="inspect-older-unloaded-result")
        assert session.until(lambda: session.view()["inventory"] == "known" and any(
            w["identity"].get("turn_id") == "unavailable-current-turn" and w["status"] == "running"
            for w in session.view()["workers"]))
        recovered = session.view()
        assert recovered["completions"] == [{"identity": identity, "outcome": outcome}]
        assert recovered["main"]["completed_turns"] == stops
        assert recovered["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert recovered["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]


@pytest.mark.parametrize(("scope", "failure"), [
    ("root", "unsupported"), ("child", "unsupported"), ("child", "missing-cursor"),
    ("child", "repeated-cursor"), ("child", "unreadable-second-page"),
    ("child", "unsupported-turn-status"), ("child", "missing-active-turn")])
def test_required_turn_history_failure_holds_existing_background_clock(monkeypatch, scope, failure):
    with ObservedSession(monkeypatch) as session:
        session.backend("create_thread", thread_id="clock-history-child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="clock-history-child", turn_id="clock-history-turn",
                        active_flags=["waitingOnUserInput"])
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        assert session.until(lambda: session.view()["inventory"] == "known" and session.view()["wait"] is not None)
        session.backend("hold_next", name="before-turn-history-fault", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "before-turn-history-fault"})
        before = session.view()
        owner = session.root if scope == "root" else "clock-history-child"
        actual = session.backend("snapshot")["threads"][owner]
        match = {"method": "thread/turns/list", "threadId": owner}
        faults = {
            "unsupported": {"error": {"code": -32601, "message": "turn history unsupported"}},
            "missing-cursor": "missing_cursor",
            "repeated-cursor": {"result": {"data": actual["turns"], "nextCursor": "opaque-never-exhausted"}},
            "unsupported-turn-status": {"result": {"data": [{"id": "clock-history-turn", "status": "unknown-status", "items": []}], "nextCursor": None}},
            "missing-active-turn": {"result": {"data": [], "nextCursor": None}},
        }
        if failure == "unreadable-second-page":
            session.backend("fault_next", matches={**match, "cursor": None}, fault={"result": {
                "data": actual["turns"], "nextCursor": "opaque-unreadable-page"}}, times=1000)
            session.backend("fault_next", matches={**match, "cursor": "opaque-unreadable-page"}, fault="error", times=1000)
        else:
            session.backend("fault_next", matches=match, fault=faults[failure], times=1000)
        reads_before = sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == owner for r in session.rows())
        session.backend("release", name="before-turn-history-fault")
        assert session.until(lambda: sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == owner for r in session.rows()) >= reads_before + 2)
        session.backend("hold_next", name="inspect-turn-history-fault", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-turn-history-fault"})
        hidden = session.view()
        assert hidden["inventory"] == "unknown"
        assert hidden["wait"] == before["wait"]
        assert hidden["completions"] == []
        assert hidden["main"]["completed_turns"] == before["main"]["completed_turns"]
        assert hidden["history_checkpoint"] == before["history_checkpoint"]
        assert session.client.retire(session.binding, hidden["revision"], reason="background",
                                     now=before["wait"]["since"] + 10801) == "held"
        session.backend("clear_faults")
        session.backend("release", name="inspect-turn-history-fault")
        assert session.until(lambda: session.view()["inventory"] == "known")
        recovered = session.view()
        assert recovered["wait"] == before["wait"]
        assert recovered["completions"] == []
        assert recovered["main"]["completed_turns"] == before["main"]["completed_turns"]
        child = next(w for w in recovered["workers"] if w["identity"].get("turn_id") == "clock-history-turn")
        assert child["status"] == "running"
        assert child["thread_status"] == "active"
        assert child["active_flags"] == ["waitingOnUserInput"]


@pytest.mark.parametrize("with_message", [False, True], ids=["itemless", "available-message"])
@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_readable_turn_outcome_survives_unreadable_item_pages(monkeypatch, status, with_message):
    with ObservedSession(monkeypatch) as session:
        session.backend("hold_next", name="discover-item-page-failure", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "discover-item-page-failure"})
        baseline = deepcopy(session.view()["history_checkpoint"])
        session.backend("create_thread", thread_id="readable-turn-child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="readable-turn-child", turn_id="readable-terminal-turn")
        session.backend("end_turn", thread_id="readable-turn-child", turn_id="readable-terminal-turn", status=status,
                        error={"message": "INDEPENDENT_TURN_FAILURE", "codexErrorInfo": "other"} if status == "failed" else None,
                        items=[{"type": "agentMessage", "id": "full-turn-message", "text": "INDEPENDENT_TURN_MESSAGE"}] if with_message else [])
        reduced = deepcopy(session.backend("snapshot")["threads"]["readable-turn-child"])
        reduced["turns"] = []
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "readable-turn-child"},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/items/list", "threadId": "readable-turn-child"},
                        fault="error", times=1000)
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        session.backend("release", name="discover-item-page-failure")
        assert session.until(lambda: sum(r["kind"] == "rpc_received" and r["method"] == "thread/read"
            and r["params"].get("threadId") == "readable-turn-child" for r in session.rows()) >= 2)
        session.backend("hold_next", name="inspect-item-page-failure", phase="before", matches={"method": "thread/list"})
        session.backend("wait", matches={"kind": "request_held", "name": "inspect-item-page-failure"})
        hidden = session.view()
        assert hidden["inventory"] == "unknown"
        assert session.client.retire(session.binding, hidden["revision"], reason="stopped") == "held"
        identity = {"kind": "agent", "thread_id": "readable-turn-child", "turn_id": "readable-terminal-turn"}
        expected = {"identity": identity, "outcome": {"status": status,
                    "messages": [{"item_id": "full-turn-message", "text": "INDEPENDENT_TURN_MESSAGE"}] if with_message else [],
                    "error": {"message": "INDEPENDENT_TURN_FAILURE", "codex_error_info": "other"} if status == "failed" else None}}
        assert hidden["completions"] == [expected]
        assert hidden["main"]["status"] == "stopped"
        stops = deepcopy(hidden["main"]["completed_turns"])
        assert list(stops) == [session.initial_turn]
        assert hidden["wait"] is None
        assert hidden["history_checkpoint"]["baseline_turns"] == baseline["baseline_turns"]
        assert hidden["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]
        session.backend("clear_faults")
        session.backend("release", name="inspect-item-page-failure")
        assert session.until(lambda: session.view()["inventory"] == "known")
        recovered = session.view()
        assert recovered["completions"] == [expected]
        assert recovered["main"]["completed_turns"] == stops
        assert recovered["wait"] is None
        child = next(w for w in recovered["workers"] if w["identity"] == identity)
        assert child["thread_status"] == ("systemError" if status == "failed" else "idle")
        assert child["active_flags"] == []
