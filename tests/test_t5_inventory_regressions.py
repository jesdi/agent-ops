"""Additional regressions at the agreed runtime and executable provider seams."""
from t5_runtime_support import RuntimeCase, agent, command, qualified, stored
from t5_executable_support import worker, result
from t5_observed_session import ObservedSession


def test_uncertain_new_work_does_not_reset_stopped_clock(tmp_path):
    case = RuntimeCase(tmp_path)
    first = qualified(case)[0]
    case.inventory([first, agent(case)], certainty="unknown", now=300)
    assert case.view()["wait"]["since"] == 100
    assert case.view()["wait"]["ever_reported"] == [first["identity"]]


def test_disappearing_native_terminal_is_unknown_until_outcome(monkeypatch):
    with ObservedSession(monkeypatch) as session:
        session.backend("start_command", thread_id=session.root, turn_id=session.initial_turn, item_id="vanished")
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        assert session.until(lambda: (worker(session.view(), "vanished") or {}).get("eligible"))
        session.backend("omit_terminal", thread_id=session.root, item_id="vanished")
        assert session.until(lambda: session.view()["inventory"] == "unknown")
        assert result(session.view(), "vanished") is None


def test_definitive_native_failure_survives_unreadable_owner(monkeypatch):
    with ObservedSession(monkeypatch) as session:
        session.backend("start_command", thread_id=session.root, turn_id=session.initial_turn, item_id="failed")
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        assert session.until(lambda: (worker(session.view(), "failed") or {}).get("eligible"))
        session.backend("fault_next", matches={"method": "thread/read", "threadId": session.root}, fault="error", times=1000)
        session.backend("end_command", thread_id=session.root, item_id="failed", status="failed", exit_code=-1, output=None)
        assert session.until(lambda: result(session.view(), "failed") is not None)
        assert result(session.view(), "failed")["outcome"]["exit_code"] == -1
        assert session.view()["inventory"] == "unknown"


def test_postlaunch_child_failure_first_seen_in_history_is_available(tmp_path):
    from t5_runtime_support import finished, completion
    case = RuntimeCase(tmp_path)
    failed = finished(agent(case), status="failed", messages=[], error={"message": "child failed offline"})
    case.inventory([failed])
    assert completion(case.view(), failed["identity"])["outcome"] == failed["outcome"]


def test_later_interaction_cannot_seal_original_command_outcome(tmp_path):
    from t5_runtime_support import finished
    case = RuntimeCase(tmp_path)
    initial = qualified(case)[0]
    case.start("turn-B", now=200)
    interaction = finished(initial, output="interaction-only output")
    interaction.update(turn_id="turn-B", source="unifiedExecInteraction")
    case.inventory([interaction], now=210)
    original = stored(case.view(), initial["identity"])
    assert original["status"] == "running" and original["turn_id"] == "turn-A"
    assert original["outcome"] is None and case.view()["completions"] == []


def test_input_ack_before_started_keeps_old_stopped_clock_invalid(tmp_path):
    case = RuntimeCase(tmp_path)
    first = qualified(case)[0]
    assert case.control.accept_input(case.binding, "new-input")
    assert case.send({"type": "input/accepted", "client_message_id": "new-input", "turn_id": "turn-B"})
    new = command(case, "new-command")
    case.inventory([first, new], now=500)
    assert stored(case.view(), new["identity"])["eligible"]
    assert case.view()["wait"]["since"] == 100
    assert case.view()["wait"]["ever_reported"] == [first["identity"]]
    assert case.retire(now=50000) == "held"


def test_descendant_failure_and_original_command_recover_across_exact_owner_load(monkeypatch):
    with ObservedSession(monkeypatch) as session:
        session.backend("create_thread", thread_id="child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="child", turn_id="child-turn")
        session.backend("start_command", thread_id="child", turn_id="child-turn", item_id="owned-command")
        session.backend("end_turn", thread_id="child", turn_id="child-turn")
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        assert session.until(lambda: (worker(session.view(), "owned-command") or {}).get("eligible"))
        session.backend("start_turn", thread_id="child", turn_id="second-child-turn")
        session.backend("end_turn", thread_id="child", turn_id="second-child-turn", status="failed", error={"message": "native child failure"})
        session.backend("end_command", thread_id="child", item_id="owned-command", status="failed", exit_code=7, output="EXACT_OWNER_SUFFIX")
        session.backend("set_thread", thread_id="child", overrides={"status": {"type": "notLoaded"}})
        assert session.until(lambda: result(session.view(), "owned-command") is not None)
        assert session.until(lambda: any(c["identity"].get("turn_id") == "second-child-turn" for c in session.view()["completions"]))
        assert result(session.view(), "owned-command")["outcome"]["aggregated_output"] == "EXACT_OWNER_SUFFIX"
        assert session.until(lambda: any(r["kind"] == "rpc_received" and r["method"] == "thread/resume" and r["params"].get("threadId") == "child" for r in session.rows()))
        assert session.view()["binding"]["conversation_id"] == session.root


def test_reduced_turn_read_recovers_all_native_turn_pages(monkeypatch):
    from copy import deepcopy
    with ObservedSession(monkeypatch) as session:
        native = session.backend("snapshot")["threads"][session.root]
        reduced = deepcopy(native)
        reduced["turns"] = []
        session.backend("fault_next", matches={"method": "thread/read", "threadId": session.root},
                        fault={"result": {"thread": reduced}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "cursor": None},
                        fault={"result": {"data": native["turns"], "nextCursor": "opaque-turn-final"}}, times=1000)
        session.backend("fault_next", matches={"method": "thread/turns/list", "cursor": "opaque-turn-final"},
                        fault={"result": {"data": [], "nextCursor": None}}, times=1000)
        assert session.until(lambda: any(r["kind"] == "rpc_received" and r["method"] == "thread/turns/list"
                                       and r["params"].get("cursor") == "opaque-turn-final" for r in session.rows()))
        assert session.until(lambda: session.view()["inventory"] == "known")
        assert session.view()["main"]["status"] == "active"
        assert session.view()["completions"] == []


def test_unqualified_running_command_holds_cap_for_other_eligible_work(tmp_path):
    case = RuntimeCase(tmp_path)
    first = qualified(case)[0]
    unknown_owner_end = command(case, "unqualified", turn="unseen-turn", normal_stop=None)
    case.inventory([first, unknown_owner_end])
    assert case.retire(now=50000) == "held"


def test_definitive_child_error_survives_unreadable_history(monkeypatch):
    with ObservedSession(monkeypatch) as session:
        session.backend("create_thread", thread_id="error-child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="error-child", turn_id="error-turn")
        assert session.until(lambda: any(w["identity"].get("turn_id") == "error-turn" for w in session.view()["workers"]))
        session.backend("fault_next", matches={"method": "thread/read", "threadId": "error-child"}, fault="error", times=1000)
        session.backend("end_turn", thread_id="error-child", turn_id="error-turn", status="failed", error={"message": "definitive child error"})
        assert session.until(lambda: any(c["identity"].get("turn_id") == "error-turn" for c in session.view()["completions"]))
        assert session.view()["main"]["status"] == "active"
        assert session.view()["inventory"] == "unknown"
