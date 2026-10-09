"""Actual supervisor/BoundClient/product listener/native provider boundary."""

from copy import deepcopy

import pytest

from t5_executable_support import ExecutableSession, result, worker
from t5_runtime_support import covers, identities


@pytest.fixture(autouse=True)
def criterion_property(request, record_property):
    record_property("t5_criteria", ",".join(getattr(request.function, "t5_criteria", ())))


def stopped_background(session, *items):
    for item in items:
        session.backend("start_command", thread_id=session.root, turn_id=session.initial_turn,
                        item_id=item, process_id="shared-process")
    session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
    assert session.until(lambda: all((worker(session.view(), item) or {}).get("eligible") for item in items)), (
        "native complete post-Stop command survival was not normalized into eligible owned workers")
    assert session.view()["wait"] is not None


def prior_native_history_script(*, unreadable=False):
    actions = [
        {"action": "create_thread", "thread_id": "root-fixture"},
        {"action": "start_turn", "thread_id": "root-fixture", "turn_id": "old-root-turn"},
        {"action": "start_command", "thread_id": "root-fixture", "turn_id": "old-root-turn", "item_id": "old-completed"},
        {"action": "end_command", "thread_id": "root-fixture", "item_id": "old-completed", "status": "failed", "exit_code": 7, "output": "OLD_NATIVE_SUFFIX"},
        {"action": "start_command", "thread_id": "root-fixture", "turn_id": "old-root-turn", "item_id": "old-running"},
        {"action": "end_turn", "thread_id": "root-fixture", "turn_id": "old-root-turn"},
        {"action": "create_thread", "thread_id": "old-child", "parent": "root-fixture", "depth": 1},
        {"action": "start_turn", "thread_id": "old-child", "turn_id": "old-child-turn"},
        {"action": "hold_next", "name": "bootstrap-before-accept", "phase": "before", "matches": {"method": "turn/start"}},
    ]
    if unreadable:
        actions.append({"action": "fault_next", "matches": {"method": "thread/backgroundTerminals/list", "threadId": "root-fixture"},
                        "fault": "error", "times": 1000})
    return actions


@covers("H01", "P01", "P02", "P03")
def test_fresh_physical_resume_seeds_native_old_history_and_work_before_bootstrap_prompt():
    with ExecutableSession(script=prior_native_history_script(), resume="root-fixture", bootstrap_pending=True) as session:
        session.backend("wait", matches={"kind": "request_held", "name": "bootstrap-before-accept"})
        snapshot = session.view()
        mark = snapshot.get("history_checkpoint")
        assert mark is not None, "native history/work baseline must be durably established before forwarding bootstrap prompt"
        assert mark["seeded"] is True and mark["launch_id"] == session.binding["launch_id"]
        expected_turns = [{"thread_id": session.root, "turn_id": "old-root-turn"},
                          {"thread_id": "old-child", "turn_id": "old-child-turn"}]
        assert identities(expected_turns) <= identities(mark["baseline_turns"])
        old_workers = [{"kind": "command", "thread_id": session.root, "initial_item_id": "old-completed"},
                       {"kind": "command", "thread_id": session.root, "initial_item_id": "old-running"},
                       {"kind": "agent", "thread_id": "old-child", "turn_id": "old-child-turn"}]
        assert identities(old_workers) <= identities(mark["baseline_workers"])
        assert snapshot["completions"] == [] and snapshot["wait"] is None
        rows = session.rows()
        prompt_index = next(i for i, row in enumerate(rows) if row["kind"] == "rpc_received" and row["method"] == "turn/start")
        before = rows[:prompt_index]
        terminal_responses = [r for r in before if r["kind"] == "rpc_response" and r["method"] == "thread/backgroundTerminals/list"]
        assert terminal_responses and any(r["packet"].get("result", {}).get("nextCursor") is None for r in terminal_responses), "baseline terminal scan must finish before prompt"
        # Turn/read's full native root history is a supported alternative to
        # paginated item history. Child identity still requires exact child scope.
        child_responses = [r for r in before if r["kind"] == "rpc_response" and r["method"] in {"thread/read", "thread/resume"}
                           and r["packet"].get("result", {}).get("thread", {}).get("id") == "old-child"]
        assert child_responses, "old child turn must be read before baseline excludes its result"
        session.backend("release", name="bootstrap-before-accept")
        session.finish_start()
        session.backend("end_command", thread_id=session.root, item_id="old-running", status="failed", exit_code=7, output="OLD_OFFLINE_SUFFIX")
        session.backend("end_turn", thread_id="old-child", turn_id="old-child-turn", items=[
            {"type": "agentMessage", "id": "old-child-result", "text": "OLD_CHILD_RESULT"}])
        stopped_background(session, "current-launch-command")
        assert not identities(c["identity"] for c in session.view()["completions"]) & identities(old_workers)
        assert identities(session.view()["history_checkpoint"]["baseline_workers"]) == identities(mark["baseline_workers"])
        session.backend("end_command", thread_id=session.root, item_id="current-launch-command", exit_code=0, output="CURRENT_SUFFIX")
        assert session.until(lambda: result(session.view(), "current-launch-command") is not None)
        assert len(session.view()["completions"]) == 1


@covers("H02", "P04")
def test_unreadable_native_baseline_before_bootstrap_cannot_be_seeded_as_empty():
    with ExecutableSession(script=prior_native_history_script(unreadable=True), resume="root-fixture", bootstrap_pending=True) as session:
        assert session.until(lambda: any(
            (r["kind"] == "request_held" and r.get("name") == "bootstrap-before-accept") or
            (r["kind"] == "rpc_response" and r["method"] == "thread/backgroundTerminals/list" and "error" in r["packet"])
            for r in session.rows())), "native seed query or held bootstrap must reach its declared observation barrier"
        snapshot = session.view()
        mark = snapshot.get("history_checkpoint")
        assert mark is not None, "unreadable native seed must expose a retained unseeded checkpoint before bootstrap"
        assert mark["seeded"] is False and mark["launch_id"] == session.binding["launch_id"]
        assert snapshot["inventory"] == "unknown" and snapshot["completions"] == [] and snapshot["wait"] is None
        assert snapshot["alerts"], "unreadable required native seed must report compatibility/control problem"
        assert not any(r["kind"] == "rpc_response" and r["method"] == "turn/start" for r in session.rows())
        session.backend("clear_faults")
        session.backend("release", name="bootstrap-before-accept")
        assert session.until(session.terminal_control.exists), "readable retry and released bootstrap must attach the existing task terminal"
        assert session.terminal("snapshot")["root"] == session.root


@covers("C01", "C02", "C08", "C09", "P02", "U07", "H06")
def test_native_foreground_and_two_surviving_commands_are_distinguished_without_yield_receipts():
    with ExecutableSession() as session:
        for item in ["foreground", "background-A", "background-B"]:
            session.backend("start_command", thread_id=session.root, turn_id=session.initial_turn, item_id=item)
        native = session.backend("snapshot")["threads"][session.root]
        turn = next(t for t in native["turns"] if t["id"] == session.initial_turn)
        assert not any(i["type"] == "commandExecution" for i in turn["items"]), "fixture pending commands must be absent from persisted native history"
        assert session.until(lambda: worker(session.view(), "foreground") is not None), "native active command observation missing"
        assert not worker(session.view(), "foreground")["eligible"] and session.view()["wait"] is None
        session.backend("end_command", thread_id=session.root, item_id="foreground", exit_code=0, output="FG_END")
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        assert session.until(lambda: all((worker(session.view(), i) or {}).get("eligible") for i in ["background-A", "background-B"])), "survivors absent after paginated exact-root inventory"
        assert result(session.view(), "foreground") is None
        since = session.view()["wait"]["since"]
        session.backend("end_command", thread_id=session.root, item_id="background-A", exit_code=0, output="BG_END")
        assert session.until(lambda: result(session.view(), "background-A") is not None), "available partial completion was not collected promptly"
        assert worker(session.view(), "background-B")["status"] == "running"
        assert session.view()["wait"]["since"] == since
        queried = [row["params"] for row in session.rows() if row["kind"] == "rpc_received" and row["method"] == "thread/backgroundTerminals/list"]
        assert any(params.get("cursor") is not None for params in queried), "second terminal page must be queried"


@covers("C03")
def test_native_terminal_reply_started_before_main_stop_cannot_qualify_on_arrival():
    with ExecutableSession() as session:
        session.backend("start_command", thread_id=session.root, turn_id=session.initial_turn, item_id="race-command")
        session.backend("hold_next", name="pre-stop", matches={"method": "thread/backgroundTerminals/list", "threadId": session.root})
        session.backend("wait", matches={"kind": "request_held", "name": "pre-stop"})
        session.backend("hold_next", name="next-post-stop", matches={"method": "thread/backgroundTerminals/list", "threadId": session.root})
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        assert session.until(lambda: session.view()["main"]["status"] == "stopped")
        session.backend("release", name="pre-stop")
        session.backend("wait", matches={"kind": "request_held", "name": "next-post-stop"})
        observed = worker(session.view(), "race-command")
        assert observed is not None, "pre-query causal observation must be stored without eligibility"
        assert not observed["eligible"] and session.view()["wait"] is None
        session.backend("release", name="next-post-stop")
        assert session.until(lambda: (worker(session.view(), "race-command") or {}).get("eligible")), "fresh post-Stop query must establish eligibility"


@covers("O01", "O02", "O03", "O05", "O07", "P01")
def test_native_descendant_pages_count_active_depth8_through_idle_parents_and_exclude_foreign_threads():
    with ExecutableSession() as session:
        parent = session.root
        for depth in range(1, 9):
            leaf = "owned-" + str(depth)
            session.backend("create_thread", thread_id=leaf, parent=parent, depth=depth)
            session.backend("start_turn", thread_id=leaf, turn_id=leaf + "-turn")
            if depth < 8:
                session.backend("end_turn", thread_id=leaf, turn_id=leaf + "-turn")
            parent = leaf
        session.backend("create_thread", thread_id="foreign-root")
        session.backend("create_thread", thread_id="foreign-child", parent="foreign-root", depth=1)
        session.backend("start_turn", thread_id="foreign-child", turn_id="foreign-turn")
        session.backend("create_thread", thread_id="title", source={"subAgent": "review"})
        session.backend("notification", method="item/completed", params={"threadId": session.root,
            "turnId": session.initial_turn, "completedAtMs": 1791301854000,
            "item": {"type": "subAgentActivity", "id": "spawn-item", "kind": "started",
                     "agentThreadId": "owned-8", "agentPath": "/root/owned"}})
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        identity = {"kind": "agent", "thread_id": "owned-8", "turn_id": "owned-8-turn"}
        assert session.until(lambda: any(w["identity"] == identity and w["eligible"] for w in session.view()["workers"])), "complete all-depth owned child inventory missing"
        assert identity in session.view()["wait"]["ever_reported"]
        assert all(w["identity"]["thread_id"] not in {"foreign-child", "title"} for w in session.view()["workers"])
        assert session.view()["binding"]["conversation_id"] == session.root
        assert session.view()["main"]["turn_id"] == session.initial_turn
        rows = [r for r in session.rows() if r["kind"] == "rpc_received" and r["method"] == "thread/list"]
        assert rows and any(r["params"].get("cursor") is not None for r in rows), "complete discovery requires native pages"


@covers("O09", "O10", "O11", "O12", "P02")
def test_native_nested_command_needs_own_child_end_before_exact_owner_terminal_query():
    with ExecutableSession() as session:
        session.backend("create_thread", thread_id="child", parent=session.root, depth=1)
        session.backend("create_thread", thread_id="grandchild", parent="child", depth=2)
        session.backend("start_turn", thread_id="child", turn_id="parent-turn")
        session.backend("end_turn", thread_id="child", turn_id="parent-turn")
        session.backend("start_turn", thread_id="grandchild", turn_id="owner-turn")
        session.backend("start_command", thread_id="grandchild", turn_id="owner-turn", item_id="nested-command")
        session.backend("end_turn", thread_id=session.root, turn_id=session.initial_turn)
        assert session.until(lambda: worker(session.view(), "nested-command", "grandchild") is not None), "exact descendant terminal observation missing"
        assert not worker(session.view(), "nested-command", "grandchild")["eligible"]
        session.backend("hold_next", name="pre-child-stop", matches={"method": "thread/backgroundTerminals/list", "threadId": "grandchild"})
        session.backend("wait", matches={"kind": "request_held", "name": "pre-child-stop"})
        session.backend("hold_next", name="after-child-stop", matches={"method": "thread/backgroundTerminals/list", "threadId": "grandchild"})
        session.backend("end_turn", thread_id="grandchild", turn_id="owner-turn")
        session.backend("release", name="pre-child-stop")
        session.backend("wait", matches={"kind": "request_held", "name": "after-child-stop"})
        assert not worker(session.view(), "nested-command", "grandchild")["eligible"], "reply arrival cannot fabricate causal child-end survival"
        session.backend("release", name="after-child-stop")
        assert session.until(lambda: (worker(session.view(), "nested-command", "grandchild") or {}).get("eligible")), "own normal child Stop must qualify exact-owner surviving command"
        stop = worker(session.view(), "nested-command", "grandchild")["qualifying_stop"]
        assert stop["thread_id"] == "grandchild" and stop["turn_id"] == "owner-turn"
        assert session.view()["main"]["turn_id"] == session.initial_turn


@covers("I03", "U04", "U05", "O13", "P03")
def test_native_later_stdin_and_failed_child_preserve_available_outcomes_without_output_reconstruction():
    with ExecutableSession() as session:
        stopped_background(session, "initial-item")
        initial = deepcopy(worker(session.view(), "initial-item"))
        operator = session.terminal("rpc", method="turn/start", params={"threadId": session.root,
            "clientUserMessageId": "operator-generic", "input": [{"type": "text", "text": "generic operator input"}]})
        turn_b = operator["result"]["turn"]["id"]
        session.backend("interaction", thread_id=session.root, turn_id=turn_b, item_id="initial-item")
        session.backend("output_delta", thread_id=session.root, item_id="initial-item", delta="PING")
        native_outcome = session.backend("end_command", thread_id=session.root, item_id="initial-item", status="failed",
                                        exit_code=7, output="PINGFINAL_SUFFIX")
        session.backend("notification", method="item/completed", params={"threadId": session.root,
            "turnId": session.initial_turn, "completedAtMs": 1791301854000, "item": native_outcome})
        session.backend("create_thread", thread_id="failed-child", parent=session.root, depth=1)
        session.backend("start_turn", thread_id="failed-child", turn_id="failed-turn")
        assert session.until(lambda: any(w["identity"]["thread_id"] == "failed-child" for w in session.view()["workers"])), "child start must be observed before failure"
        session.backend("end_turn", thread_id="failed-child", turn_id="failed-turn", status="failed",
                        error={"message": "FIXTURE_CHILD_FAILURE", "codexErrorInfo": "other"})
        assert session.until(lambda: result(session.view(), "initial-item") is not None), "native command outcome absent"
        outcome = result(session.view(), "initial-item")["outcome"]
        assert outcome["aggregated_output"] == "PINGFINAL_SUFFIX" and outcome["exit_code"] == 7
        assert worker(session.view(), "initial-item")["turn_id"] == initial["turn_id"]
        assert session.until(lambda: any(c["identity"]["thread_id"] == "failed-child" for c in session.view()["completions"])), "definitive failed child error with empty history must survive"
        child = next(c for c in session.view()["completions"] if c["identity"]["thread_id"] == "failed-child")
        assert child["outcome"]["status"] == "failed" and child["outcome"]["error"]["message"] == "FIXTURE_CHILD_FAILURE"
        for number in range(2):
            session.backend("notification", method="item/completed", params={"threadId": session.root,
                "turnId": turn_b, "completedAtMs": 1791301854000,
                "item": {"type": "subAgentActivity", "id": "child-result-" + str(number), "kind": "completed",
                         "agentThreadId": "failed-child", "agentPath": "/root/failed-child"}})
        assert session.view()["main"]["turn_id"] == turn_b and session.view()["main"]["status"] == "active"
        assert len([c for c in session.view()["completions"] if c["identity"] == initial["identity"]]) == 1
        assert len([c for c in session.view()["completions"] if c["identity"]["thread_id"] == "failed-child"]) == 1


@covers("P04", "P07", "H05")
@pytest.mark.parametrize("method", ["thread/list", "thread/backgroundTerminals/list", "thread/items/list"])
@pytest.mark.parametrize("fault", ["missing_cursor", "repeat_cursor", "error", {"result": {"data": [None], "nextCursor": None}}], ids=["missing-cursor", "repeated-cursor", "query-error", "malformed-row"])
def test_native_unreadable_page_cannot_erase_known_work_clock_or_checkpoint(method, fault):
    with ExecutableSession() as session:
        stopped_background(session, "known-command", "other-known-command")
        since = session.view()["wait"]["since"]
        mark = deepcopy(session.view().get("history_checkpoint"))
        assert mark is not None, "authoritative launch history checkpoint must be exposed"
        for child in ["page-child-A", "page-child-B"]:
            session.backend("create_thread", thread_id=child, parent=session.root, depth=1)
        if method == "thread/items/list":
            native = session.backend("snapshot")["threads"][session.root]
            native["turns"][0]["items"].extend([
                {"type": "userMessage", "id": "history-user-" + str(i), "clientId": None,
                 "content": [{"type": "text", "text": "generic retained user input"}]} for i in range(2)])
            session.backend("set_thread", thread_id=session.root, overrides={"turns": native["turns"]})
            session.backend("history_view", thread_id=session.root, items_view="summary")
        session.backend("fault_next", matches={"method": method}, fault=fault, times=1000)
        assert session.until(lambda: session.view()["inventory"] == "unknown"), "unreadable/incomplete scan must become unknown"
        assert worker(session.view(), "known-command")["eligible"] and session.view()["completions"] == []
        assert session.view()["wait"]["since"] == since
        assert session.view()["history_checkpoint"]["baseline_workers"] == mark["baseline_workers"]
        assert session.view()["alerts"], "unsupported required interface must expose a compatibility problem"


@covers("P03", "U01", "U07", "H03", "H06")
def test_offline_outcomes_on_final_native_item_pages_require_complete_scoped_cursors():
    with ExecutableSession() as session:
        stopped_background(session, "offline-page-A", "offline-page-B")
        native = session.backend("snapshot")["threads"][session.root]
        assert all(i["type"] != "commandExecution" for t in native["turns"] for i in t["items"])
        since = session.view()["wait"]["since"]
        session.backend("history_view", thread_id=session.root, items_view="summary")
        peers = session.backend("snapshot")["peers"]
        controller = next(p for p in peers if p["client_name"] != "fixture-terminal" and session.root in p["subscriptions"])
        session.backend("disconnect", peer=controller["id"])
        for item in ["offline-page-A", "offline-page-B"]:
            session.backend("end_command", thread_id=session.root, item_id=item, exit_code=7,
                            status="failed", output="FINAL_PAGE_" + item)
        assert session.until(lambda: all(result(session.view(), i) is not None for i in ["offline-page-A", "offline-page-B"]), seconds=12), "offline native final-page outcomes must be collected through reduced-history item pagination"
        assert session.view()["wait"]["since"] == since
        for item in ["offline-page-A", "offline-page-B"]:
            found = [c for c in session.view()["completions"] if c["identity"].get("initial_item_id") == item]
            assert len(found) == 1 and found[0]["outcome"]["aggregated_output"] == "FINAL_PAGE_" + item
        rows = session.rows()
        requests = {(r["peer"], r["request_id"]): r for r in rows if r["kind"] == "rpc_received"}
        pages = [r for r in rows if r["kind"] == "rpc_response" and r["method"] == "thread/items/list"]
        assert any(requests[(r["peer"], r["packet"]["id"])]["params"].get("cursor") is not None for r in pages), "cursor continuation must reach native final-page outcome"
        assert any(r["packet"].get("result", {}).get("nextCursor", "missing") is None and
                   any(v["item"]["id"] in {"offline-page-A", "offline-page-B"} for v in r["packet"].get("result", {}).get("data", [])) for r in pages)


@covers("P04", "P02", "P07")
def test_paused_nonfinal_native_terminal_page_keeps_known_clock_and_cannot_claim_empty():
    with ExecutableSession() as session:
        stopped_background(session, "page-running-A", "page-running-B")
        since = session.view()["wait"]["since"]
        session.backend("hold_next", name="nonfinal-page", matches={"method": "thread/backgroundTerminals/list", "threadId": session.root})
        session.backend("wait", matches={"kind": "request_held", "name": "nonfinal-page"})
        session.backend("hold_next", name="final-page", matches={"method": "thread/backgroundTerminals/list", "threadId": session.root})
        session.backend("release", name="nonfinal-page")
        held = session.backend("wait", matches={"kind": "request_held", "name": "final-page"})
        assert held[-1]["params"].get("cursor") is not None, "second owner page must continue exact opaque cursor"
        partial = session.view()
        assert partial["wait"]["since"] == since and partial["completions"] == []
        assert all((worker(partial, item) or {}).get("status") == "running" for item in ["page-running-A", "page-running-B"])
        revision = partial["revision"]
        assert session.client.retire(session.binding, revision, reason="stopped") == "held"
        session.backend("release", name="final-page")
        assert session.until(lambda: all((worker(session.view(), item) or {}).get("eligible") for item in ["page-running-A", "page-running-B"]))


@covers("H03", "H05", "H06", "W04", "B08")
def test_same_controller_reconnect_recovers_offline_original_outcome_with_same_terminal_and_clock():
    with ExecutableSession() as session:
        stopped_background(session, "offline-command")
        before_native = session.backend("snapshot")["threads"][session.root]
        assert all(i["id"] != "offline-command" for t in before_native["turns"] for i in t["items"])
        assert worker(session.view(), "offline-command")["status"] == "running"
        since = session.view()["wait"]["since"]
        baseline = deepcopy(session.view().get("history_checkpoint"))
        assert baseline is not None
        attached = session.terminal("snapshot")
        operator = session.terminal("rpc", method="turn/start", params={"threadId": session.root,
            "clientUserMessageId": "operator-input", "input": [{"type": "text", "text": "generic active input"}]})
        turn_b = operator["result"]["turn"]["id"]
        peers = session.backend("snapshot")["peers"]
        controller = next(p for p in peers if p["client_name"] != "fixture-terminal" and session.root in p["subscriptions"])
        session.backend("disconnect", peer=controller["id"])
        session.backend("end_command", thread_id=session.root, item_id="offline-command", exit_code=7,
                        status="failed", output="OFFLINE_NATIVE_SUFFIX")
        assert session.until(lambda: result(session.view(), "offline-command") is not None, seconds=12), "same-controller reconnect failed to collect original outcome history"
        assert result(session.view(), "offline-command")["outcome"]["aggregated_output"] == "OFFLINE_NATIVE_SUFFIX"
        assert session.view()["wait"]["since"] == since
        assert session.view()["history_checkpoint"]["baseline_workers"] == baseline["baseline_workers"]
        after = session.terminal("snapshot")
        assert after["pid"] == attached["pid"] and after["root"] == attached["root"]
        assert session.view()["main"]["turn_id"] == turn_b
        session.restart_listener()
        assert session.view()["wait"]["since"] == since
        assert result(session.view(), "offline-command") is not None
