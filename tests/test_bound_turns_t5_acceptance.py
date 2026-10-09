"""Locked acceptance at the approved public RuntimeControl boundary.

Every mutation here is a declared event/prepare/input/retire operation, except the
explicitly approved owned legacy-Wait fixture intake. No controller internals,
production bodies, prior harnesses or tests informed these observations.
"""

from copy import deepcopy
import json

import pytest

from dispatcher.runtime_control import RuntimeControl
from t5_runtime_support import (
    RuntimeCase, agent, chain, checkpoint, command, completion, covers, finished,
    identities, native_worker, qualified, stored,
)


@pytest.fixture(autouse=True)
def criterion_property(request, record_property):
    record_property("t5_criteria", ",".join(getattr(request.function, "t5_criteria", ())))


@covers("C01", "C02", "C09")
def test_command_requires_complete_post_owning_stop_inventory(tmp_path):
    case = RuntimeCase(tmp_path)
    early = command(case)
    case.inventory([early])
    before = stored(case.view(), early["identity"])
    assert before is not None, "active command observations must be stored without premature qualification"
    assert before["eligible"] is False and case.view()["wait"] is None
    case.stop(now=200)
    survivor = command(case)
    case.inventory([survivor], now=210)
    after = stored(case.view(), survivor["identity"])
    assert after["eligible"] is True
    assert after["qualifying_stop"] == survivor["normal_stop"]
    assert case.view()["wait"]["since"] == 210


@covers("C03", "C04")
@pytest.mark.parametrize("revision", ["before-stop", "omitted"], ids=["pre-query-revision", "missing-revision"])
def test_late_reply_cannot_retroactively_qualify_root_command(tmp_path, revision):
    case = RuntimeCase(tmp_path)
    begun = case.view()["revision"]
    case.stop(now=200)
    candidate = command(case)
    case.inventory([candidate], now=210, revision=begun if revision == "before-stop" else None)
    snapshot = case.view()
    observed = stored(snapshot, candidate["identity"])
    assert observed is not None, "unqualified observations must remain visible"
    assert not observed["eligible"]
    assert snapshot["wait"] is None and snapshot["completions"] == []
    case.inventory([candidate], now=220)
    assert stored(case.view(), candidate["identity"])["eligible"]
    assert case.view()["wait"]["since"] == 220


@covers("C05", "O10")
@pytest.mark.parametrize("mismatch", ["foreign", "old-turn", "child", "failed", "interrupted", "missing"],
                         ids=["foreign-root", "stale-main", "child-as-main", "failed-end", "interrupted-end", "no-stop"])
def test_wrong_stop_does_not_qualify_new_root_work(tmp_path, mismatch):
    case = RuntimeCase(tmp_path)
    existing = qualified(case)[0]
    assert stored(case.view(), existing["identity"])["eligible"], "positive qualification precondition"
    candidate = command(case, "exec-new")
    candidate["normal_stop"] = deepcopy(existing["normal_stop"])
    if mismatch == "missing":
        candidate["normal_stop"] = None
    elif mismatch in {"foreign", "child"}:
        candidate["normal_stop"]["thread_id"] = "other-root" if mismatch == "foreign" else "child"
    elif mismatch == "old-turn":
        candidate["normal_stop"]["turn_id"] = "never-current-turn"
    else:
        candidate["normal_stop"]["status"] = mismatch
    case.inventory([existing, candidate], now=200)
    observed = stored(case.view(), candidate["identity"], optional=True)
    assert observed is None or not observed["eligible"]
    assert case.view()["wait"]["since"] == 100
    assert case.retire(now=200, reason="stopped") == "held"


@covers("C06", "C07", "W02")
def test_accepted_old_stop_provenance_survives_new_active_main(tmp_path):
    case = RuntimeCase(tmp_path)
    case.stop(now=100)
    original_stop = deepcopy(case.stops["turn-A"])
    case.start("turn-B", now=150)
    survivor = command(case, normal_stop=original_stop)
    case.inventory([survivor], now=160)
    assert stored(case.view(), survivor["identity"])["eligible"]
    assert case.view()["main"]["status"] == "active" and case.view()["wait"] is None
    outcome = finished(survivor)
    case.inventory([outcome], now=170)
    assert completion(case.view(), survivor["identity"])["outcome"] == outcome["outcome"]
    assert case.view()["main"]["turn_id"] == "turn-B"
    assert case.retire(now=50000) == "held"


@covers("C08")
def test_foreground_history_outcome_is_retained_without_redelivery(tmp_path):
    case = RuntimeCase(tmp_path)
    foreground = command(case)
    case.inventory([foreground])
    case.inventory([finished(foreground, output="FG_END")], now=110)
    case.stop(now=120)
    case.inventory([finished(foreground, output="FG_END")], now=130)
    worker = stored(case.view(), foreground["identity"])
    assert worker is not None and worker["outcome"]["aggregated_output"] == "FG_END"
    assert not worker["eligible"] and case.view()["completions"] == []
    assert case.retire(reason="stopped", now=140) == "retired"


@covers("I01", "I02", "I03")
def test_command_identity_is_owner_and_initial_item_with_permanent_turn(tmp_path):
    case = RuntimeCase(tmp_path)
    first, second = qualified(case, "exec-A", "exec-B")
    stop = {"thread_id": "grandchild", "turn_id": "own-turn", "status": "completed",
            "evidence_source": "authoritative-child-history"}
    nested = command(case, "exec-A", owner="grandchild", turn="own-turn", depth=2, normal_stop=stop)
    case.inventory([first, second, nested], now=200)
    snapshot = case.view()
    assert len(snapshot["workers"]) == 3, "process/command/cwd equality cannot collapse typed identities"
    labels = snapshot["wait"]["workers"]
    assert len(set(labels)) == 3 and all(isinstance(label, str) and label for label in labels)
    changed = deepcopy(first)
    changed.update({"process_id": "replacement-process", "command": "different command", "cwd": "/tmp/other"})
    case.inventory([changed, second, nested], now=250)
    assert len(case.view()["workers"]) == 3
    assert set(case.view()["wait"]["workers"]) == set(labels)
    assert stored(case.view(), first["identity"])["turn_id"] == "turn-A"
    assert case.view()["wait"]["since"] == 200


@covers("I04")
def test_unlinked_interaction_source_is_not_a_new_background_worker(tmp_path):
    case = RuntimeCase(tmp_path)
    original = qualified(case)[0]
    assert stored(case.view(), original["identity"])["eligible"]
    interaction = command(case, "interaction-item", source="unifiedExecInteraction")
    case.inventory([original, interaction], now=200)
    observed = stored(case.view(), interaction["identity"], optional=True)
    assert observed is None or not observed["eligible"]
    assert case.view()["wait"]["since"] == 100
    assert case.view()["completions"] == []


@covers("C05")
def test_normal_stop_fields_cannot_qualify_a_root_turn_that_never_authoritatively_ended(tmp_path):
    case = RuntimeCase(tmp_path)
    old = qualified(case)[0]
    case.start("turn-B", now=200)
    candidate = command(case, "new-command", turn="turn-B", normal_stop={
        "thread_id": case.root, "turn_id": "turn-B", "status": "completed", "revision": case.view()["revision"]})
    case.inventory([old, candidate], now=210)
    observed = stored(case.view(), candidate["identity"], optional=True)
    assert observed is None or not observed["eligible"]
    assert case.view()["main"]["turn_id"] == "turn-B" and case.view()["main"]["status"] != "stopped"
    assert case.view()["wait"]["since"] == 100
    assert stored(case.view(), old["identity"])["eligible"] and case.retire(now=50000) != "retired"


@covers("I05", "O02", "W05")
def test_owned_child_turns_are_distinct_new_work(tmp_path):
    case = RuntimeCase(tmp_path)
    case.stop()
    first = agent(case)
    case.inventory([first], now=100)
    assert stored(case.view(), first["identity"])["eligible"]
    case.inventory([finished(first), agent(case, turn="child-turn-B")], now=200)
    assert len(case.view()["workers"]) == 2
    assert case.view()["wait"]["since"] == 200
    assert identities(case.view()["wait"]["ever_reported"]) == identities([first["identity"], agent(case, turn="child-turn-B")["identity"]])


@covers("I06", "U01")
def test_completion_is_monotonic_and_durable_across_duplicate_and_stale_running_reply(tmp_path):
    case = RuntimeCase(tmp_path)
    running = qualified(case)[0]
    outcome = finished(running, output="NATIVE_SUFFIX")
    case.inventory([outcome], now=200)
    expected = {"identity": running["identity"], "outcome": outcome["outcome"]}
    assert completion(case.view(), running["identity"]) == expected
    case.inventory([outcome], now=210)
    case.inventory([running], now=220, revision=case.stops["turn-A"]["revision"])
    case.inventory([], certainty="unknown", now=230)
    case.control = RuntimeControl(tmp_path)
    snapshot = case.view()
    assert snapshot["completions"] == [expected]
    worker = stored(snapshot, running["identity"])
    assert worker["status"] == "completed" and worker["outcome"] == outcome["outcome"]
    assert snapshot["wait"]["since"] == 100


@covers("I07")
@pytest.mark.parametrize("foreign", ["target", "issue", "launch", "replacement", "retired"])
def test_late_inventory_cannot_change_other_launch(tmp_path, foreign):
    case = RuntimeCase(tmp_path)
    running = qualified(case)[0]
    assert stored(case.view(), running["identity"])["eligible"]
    old_binding = deepcopy(case.binding)
    if foreign == "replacement":
        case.binding = case.control.prepare("fixture", 501, "implement", ticket="next", conversation_id=case.root)["binding"]
    elif foreign == "retired":
        assert case.retire(reason="forced") == "retired"
    else:
        old_binding[foreign if foreign != "launch" else "launch_id"] = 999 if foreign == "issue" else "foreign"
    before = case.view()
    accepted = case.control.event(old_binding, {"type": "inventory", "certainty": "known",
        "workers": [finished(running)], "observed_revision": before["revision"]}, now=999)
    assert accepted is False and case.view() == before


@covers("O03")
@pytest.mark.parametrize("depth", [1, 2, 3, 8])
def test_owned_descendants_at_every_depth_count(tmp_path, depth):
    case = RuntimeCase(tmp_path)
    case.stop()
    child = agent(case, owner="leaf", depth=depth)
    case.inventory([child])
    worker = stored(case.view(), child["identity"])
    assert worker is not None and worker["eligible"] and worker["status"] == "running"
    assert case.view()["wait"]["ever_reported"] == [child["identity"]]
    assert case.retire(reason="stopped", now=101) == "held"


@covers("O04", "P06", "H06")
def test_notloaded_or_absent_worker_remains_unresolved_not_success(tmp_path):
    case = RuntimeCase(tmp_path)
    case.stop()
    child = agent(case, owner="grandchild", depth=2)
    case.inventory([child])
    assert stored(case.view(), child["identity"])["eligible"]
    unreadable = deepcopy(child)
    unreadable.update({"thread_status": "notLoaded", "status": "unknown"})
    case.inventory([unreadable], certainty="unknown", now=200)
    case.inventory([], now=300)
    snapshot = case.view()
    assert stored(snapshot, child["identity"]) is not None
    assert snapshot["inventory"] == "unknown" and snapshot["completions"] == []
    assert snapshot["wait"]["since"] == 100
    assert case.retire(reason="stopped", now=20000) != "retired"
    assert case.retire(now=20000) != "retired"


@covers("O05", "O06")
@pytest.mark.parametrize("corruption", ["foreign-root", "other-source", "parent-disagrees", "missing-parent", "wrong-depth", "cycle"])
def test_ownership_requires_every_link_and_terminates_cycles(tmp_path, corruption):
    case = RuntimeCase(tmp_path)
    known = qualified(case)[0]
    assert stored(case.view(), known["identity"])["eligible"]
    candidate = agent(case, owner="candidate", depth=2)
    if corruption == "foreign-root":
        candidate["ancestry"] = chain("foreign-root", 2, "candidate")
    elif corruption == "other-source":
        candidate["ancestry"][0]["source_kind"] = "other"
    elif corruption == "parent-disagrees":
        candidate["ancestry"][0]["source_parent_thread_id"] = "foreign-parent"
    elif corruption == "missing-parent":
        candidate["ancestry"].pop(1)
    elif corruption == "wrong-depth":
        candidate["ancestry"][0]["depth"] = 30
    else:
        candidate["ancestry"][1].update({"parent_thread_id": "candidate", "source_parent_thread_id": "candidate"})
    case.inventory([known, candidate], now=200)
    observed = stored(case.view(), candidate["identity"], optional=True)
    assert observed is None or not observed["eligible"]
    assert case.view()["wait"]["since"] == 100
    assert candidate["identity"] not in case.view()["wait"]["ever_reported"]
    assert case.retire(reason="stopped", now=210) != "retired"


@covers("O08")
@pytest.mark.parametrize("flag", ["waitingOnApproval", "waitingOnUserInput"])
def test_child_active_flags_do_not_invent_child_completion(tmp_path, flag):
    case = RuntimeCase(tmp_path)
    case.stop()
    child = agent(case, flags=[flag])
    case.inventory([child])
    assert stored(case.view(), child["identity"])["status"] == "running"
    assert case.view()["completions"] == [] and case.retire(reason="stopped", now=200) == "held"


@covers("O09", "O10", "O11")
@pytest.mark.parametrize("evidence", ["native-child-event", "authoritative-child-history"])
def test_descendant_command_needs_its_own_normal_end_not_root_stop(tmp_path, evidence):
    case = RuntimeCase(tmp_path)
    case.stop()
    nested = command(case, owner="grandchild", turn="child-turn", depth=2)
    case.inventory([nested])
    observed = stored(case.view(), nested["identity"])
    assert observed is not None and not observed["eligible"]
    main_before = deepcopy(case.view()["main"])
    nested["normal_stop"] = {"thread_id": "grandchild", "turn_id": "child-turn", "status": "completed",
                             "evidence_source": evidence}
    case.inventory([nested], now=200, revision=None)
    worker = stored(case.view(), nested["identity"])
    assert worker["eligible"] and worker["qualifying_stop"] == nested["normal_stop"]
    assert case.view()["main"] == main_before and case.view()["wait"]["since"] == 200


@covers("O10")
@pytest.mark.parametrize("wrong", ["owner", "turn", "failed", "interrupted"])
def test_wrong_child_end_does_not_qualify_descendant_command(tmp_path, wrong):
    case = RuntimeCase(tmp_path)
    existing = qualified(case)[0]
    assert stored(case.view(), existing["identity"])["eligible"]
    stop = {"thread_id": "grandchild", "turn_id": "child-turn", "status": "completed",
            "evidence_source": "native-child-event"}
    stop["thread_id" if wrong == "owner" else "turn_id" if wrong == "turn" else "status"] = (
        "foreign" if wrong in {"owner", "turn"} else wrong)
    nested = command(case, owner="grandchild", turn="child-turn", depth=2, normal_stop=stop)
    case.inventory([existing, nested], now=200)
    worker = stored(case.view(), nested["identity"], optional=True)
    assert worker is None or not worker["eligible"]
    assert case.view()["wait"]["since"] == 100


@covers("P05", "B05")
def test_declared_empty_inventory_setup_still_allows_normal_stopped_retirement(tmp_path):
    case = RuntimeCase(tmp_path)
    assert case.send({"type": "inventory", "certainty": "known", "workers": []})
    case.stop()
    assert case.view()["inventory"] == "known"
    assert case.retire(reason="stopped", now=110) == "retired"


@covers("P07", "H04", "H05")
def test_unknown_partial_inventory_preserves_eligibility_outcome_checkpoint_and_clock(tmp_path):
    case = RuntimeCase(tmp_path)
    first, second = qualified(case, "exec-A", "exec-B")
    done = finished(first)
    mark = checkpoint(case, seen=[first["identity"]], scopes=[{
        "thread_id": case.root, "turn_id": "turn-A", "cursor": "opaque-cursor", "complete": False}])
    case.inventory([done, second], checkpoint=mark, now=200)
    assert case.view().get("history_checkpoint") == mark
    case.inventory([second], certainty="unknown", now=300)
    case.control = RuntimeControl(tmp_path)
    snapshot = case.view()
    assert snapshot["history_checkpoint"] == mark
    assert completion(snapshot, first["identity"])["outcome"] == done["outcome"]
    assert stored(snapshot, second["identity"])["eligible"]
    assert snapshot["wait"]["since"] == 100 and snapshot["inventory"] == "unknown"
    assert case.retire(now=50000) != "retired"


@covers("H01", "H02")
@pytest.mark.parametrize("seeded", [True, False], ids=["stable-seeded-baseline", "unreadable-seed"])
def test_physical_launch_baseline_excludes_old_outcomes_and_unreadable_seed_is_not_empty(tmp_path, seeded):
    case = RuntimeCase(tmp_path)
    case.stop()
    old = command(case, "old-native-item")
    mark = checkpoint(case, seeded=seeded, baseline_turns=[{"thread_id": case.root, "turn_id": "old-turn"}],
                      baseline_workers=[old["identity"]])
    case.inventory([finished(old)], checkpoint=mark, now=200, certainty="known" if seeded else "unknown")
    snapshot = case.view()
    assert snapshot.get("history_checkpoint") == mark
    assert completion(snapshot, old["identity"], optional=True) is None
    if not seeded:
        assert snapshot["inventory"] == "unknown" and case.retire(reason="stopped") != "retired"
    else:
        current = command(case, "new-native-item")
        case.inventory([current], now=210)
        assert stored(case.view(), current["identity"])["eligible"]
        assert case.view()["history_checkpoint"]["baseline_workers"] == [old["identity"]]


@covers("H04")
def test_foreign_checkpoint_cannot_replace_current_launch_checkpoint(tmp_path):
    case = RuntimeCase(tmp_path)
    running = qualified(case)[0]
    mark = checkpoint(case)
    case.inventory([running], checkpoint=mark)
    assert case.view().get("history_checkpoint") == mark
    foreign = deepcopy(mark)
    foreign["launch_id"] = "foreign-launch"
    case.inventory([running], checkpoint=foreign, now=200)
    assert case.view()["history_checkpoint"] == mark
    assert case.view()["wait"]["since"] == 100


@covers("U02", "U03", "U08", "B04")
@pytest.mark.parametrize("status,exit_code,output", [("completed", 0, ""), ("completed", None, None),
    ("failed", 7, "FAILURE_SUFFIX"), ("failed", -1, None), ("declined", None, None)],
    ids=["observed-empty", "unavailable-null", "failed-seven", "failed-minus-one", "declined"])
def test_command_available_outcomes_preserve_native_fields_and_hold_park(tmp_path, status, exit_code, output):
    case = RuntimeCase(tmp_path)
    running = qualified(case)[0]
    done = finished(running, status=status, exit_code=exit_code, output=output, duration=None)
    case.inventory([done], now=200)
    result = completion(case.view(), running["identity"])
    assert result is not None, "eligible terminal outcome must become available independently of delivery"
    assert result["outcome"] == done["outcome"]
    case.inventory([], certainty="unknown", now=300)
    assert completion(case.view(), running["identity"])["outcome"] == done["outcome"]
    assert case.retire(reason="stopped") != "retired" and case.retire() != "retired"
    assert case.view()["wait"]["since"] == 100 and not case.view()["retired"]


@covers("U05", "U06")
@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_child_outcome_preserves_exact_error_and_available_messages(tmp_path, status):
    case = RuntimeCase(tmp_path)
    case.stop()
    child = agent(case)
    case.inventory([child])
    assert stored(case.view(), child["identity"])["eligible"]
    done = finished(child, status=status, messages=[] if status == "failed" else [
        {"item_id": "agent-result", "text": "AVAILABLE_AGENT_RESULT"}],
        error={"message": "FIXTURE_CHILD_FAILURE", "codex_error_info": "other"} if status == "failed" else None)
    case.inventory([done], now=200)
    assert completion(case.view(), child["identity"])["outcome"] == done["outcome"]
    assert case.view()["main"]["turn_id"] == "turn-A" and case.retire(reason="stopped") != "retired"


@covers("U07", "W01", "W03", "W04")
def test_partial_completion_and_same_subset_reappearance_preserve_ever_reported_union(tmp_path):
    case = RuntimeCase(tmp_path)
    first, second = qualified(case, "exec-A", "exec-B")
    expected = [first["identity"], second["identity"]]
    assert identities(case.view()["wait"]["ever_reported"]) == identities(expected)
    case.inventory([finished(first), second], now=200)
    assert completion(case.view(), first["identity"]) is not None
    assert stored(case.view(), second["identity"])["status"] == "running"
    case.inventory([second], now=300)
    case.inventory([second], certainty="unknown", now=400)
    case.start("turn-B", now=500)
    case.inventory([second], now=600)
    case.stop("turn-B", now=700)
    case.inventory([second], now=800)
    case.control = RuntimeControl(tmp_path)
    assert case.view()["wait"]["since"] == 100
    assert identities(case.view()["wait"]["ever_reported"]) == identities(expected)
    assert case.retire(now=20000) != "retired", "unresolved first result retains T4 retirement hold"


@covers("W03", "W05")
def test_reappearance_of_old_child_is_not_new_but_new_initial_item_resets(tmp_path):
    case = RuntimeCase(tmp_path)
    first, second = qualified(case, "exec-A", "exec-B")
    assert case.view()["wait"]["since"] == 100
    case.inventory([second], now=200)
    case.inventory([first, second], now=300)
    assert case.view()["wait"]["since"] == 100
    new = command(case, "exec-C")
    case.inventory([first, second, new], now=400)
    assert case.view()["wait"]["since"] == 400
    assert identities(case.view()["wait"]["ever_reported"]) == identities([first["identity"], second["identity"], new["identity"]])


@covers("W02", "W04")
def test_foreground_input_and_new_active_work_do_not_reset_wait_until_next_stop(tmp_path):
    case = RuntimeCase(tmp_path)
    first = qualified(case)[0]
    assert case.view()["wait"]["since"] == 100
    assert case.control.accept_input(case.binding, "operator-message")
    assert case.view()["wait"]["since"] == 100
    case.start("turn-B", now=200)
    assert case.send({"type": "input/accepted", "client_message_id": "operator-message", "turn_id": "turn-B"}, now=200)
    child = agent(case)
    case.inventory([first, child], now=300)
    assert case.view()["wait"]["since"] == 100
    case.stop("turn-B", now=400)
    case.inventory([first, child], now=410)
    assert case.view()["wait"]["since"] == 410


@covers("W02", "W04", "W05", "B03")
def test_admitted_input_before_turn_start_cannot_report_new_work_against_old_stop(tmp_path):
    case = RuntimeCase(tmp_path)
    first = qualified(case)[0]
    assert case.view()["wait"]["since"] == 100
    original_union = identities(case.view()["wait"]["ever_reported"])
    assert case.control.accept_input(case.binding, "pending-operator-message")
    newcomer = command(case, "exec-after-admission", turn="turn-A")
    case.inventory([first, newcomer], now=300)
    assert stored(case.view(), newcomer["identity"])["eligible"], "accepted owning A Stop remains valid command provenance during input admission"
    assert case.view()["wait"]["since"] == 100, "admitted input immediately invalidates old stopped waiting authority before turn/started"
    assert identities(case.view()["wait"]["ever_reported"]) == original_union
    assert case.retire(now=50000) == "held"
    case.start("turn-B", now=400)
    assert case.send({"type": "input/accepted", "client_message_id": "pending-operator-message", "turn_id": "turn-B"}, now=400)
    case.stop("turn-B", now=500)
    case.inventory([first, newcomer], now=510)
    assert case.view()["wait"]["since"] == 510
    assert identities(case.view()["wait"]["ever_reported"]) == original_union | identities([newcomer["identity"]])


@covers("W06", "W07")
@pytest.mark.parametrize("retained", [False, True], ids=["native-clock-union", "legacy-wait-intake"])
def test_native_clock_retains_union_and_legacy_wait_missing_extension(tmp_path, retained):
    case = RuntimeCase(tmp_path, runtime="claude")
    case.stop(background_tasks=[native_worker("A"), native_worker("B")])
    if retained:
        # Specifically approved fixture intake, before restarting any listener.
        matches = [path for path in (tmp_path / "runtime").rglob("*.json")
                   if path.stem == case.binding["launch_id"]]
        assert len(matches) == 1, "approved prepared-launch UUID intake must be unambiguous"
        document = json.loads(matches[0].read_text())
        document["wait"] = {"since": 100, "workers": ["A", "B"]}
        matches[0].write_text(json.dumps(document))
        case.control = RuntimeControl(tmp_path)
    case.start("turn-B", now=200)
    case.stop("turn-B", now=210, background_tasks=[native_worker("B")])
    assert case.view()["wait"]["since"] == 100
    case.start("turn-C", now=300)
    case.stop("turn-C", now=310, background_tasks=[native_worker("A"), native_worker("B")])
    assert case.view()["wait"]["since"] == 100
    assert set(case.view()["wait"]["ever_reported"]) == {"A", "B"}
    case.start("turn-D", now=400)
    case.stop("turn-D", now=410, background_tasks=[native_worker("C")])
    assert case.view()["wait"]["since"] == 410
    assert set(case.view()["wait"]["ever_reported"]) == {"A", "B", "C"}


@covers("B01")
@pytest.mark.parametrize("cap,elapsed,expected", [(10800, 10799, "held"), (10800, 10800, "held"),
    (10800, 10801, "retired"), (30, 30, "held"), (30, 31, "retired")])
def test_background_cap_is_strict_and_configurable(tmp_path, cap, elapsed, expected):
    case = RuntimeCase(tmp_path)
    running = qualified(case)[0]
    assert stored(case.view(), running["identity"])["eligible"]
    assert case.retire(now=100 + elapsed, cap=cap) == expected
    assert case.view()["retired"] is (expected == "retired")
    if expected == "retired":
        assert not case.control.accept_input(case.binding, "too-late")


@covers("B02", "B03", "B04")
@pytest.mark.parametrize("hold", ["active", "inventory-unknown", "control-unknown", "service-unknown", "revision", "input", "pending"])
def test_background_cap_never_overrides_input_results_or_unknown_authority(tmp_path, hold):
    case = RuntimeCase(tmp_path)
    running = qualified(case)[0]
    assert stored(case.view(), running["identity"])["eligible"]
    revision = None
    if hold == "active":
        case.start("turn-B", now=200)
    elif hold == "inventory-unknown":
        case.inventory([running], certainty="unknown", now=200)
    elif hold == "control-unknown":
        case.send({"type": "control/unknown", "message": "fixture connection unreadable"}, now=200)
    elif hold == "service-unknown":
        case.send({"type": "service", "status": "unknown"}, now=200)
    elif hold == "revision":
        revision = case.view()["revision"]
        case.inventory([running], now=200)
    elif hold == "input":
        assert case.control.accept_input(case.binding, "operator-message")
    else:
        case.inventory([finished(running)], now=200)
        assert completion(case.view(), running["identity"]) is not None
    assert case.retire(now=50000, revision=revision) != "retired"
    assert not case.view()["retired"] and case.view()["wait"]["since"] == 100


@covers("B08", "B09")
def test_crash_cannot_be_capped_and_new_physical_resume_starts_fresh_clock(tmp_path):
    case = RuntimeCase(tmp_path)
    running = qualified(case)[0]
    assert stored(case.view(), running["identity"])["eligible"]
    case.send({"type": "service", "status": "dead"}, now=200)
    assert case.retire(now=50000) != "retired"
    original = deepcopy(case.binding)
    case.binding = case.control.prepare("fixture", 501, "review", conversation_id=case.root)["binding"]
    assert case.binding["launch_id"] != original["launch_id"] and case.view()["wait"] is None
    case.send({"type": "service", "status": "live"})
    case.start("resume-turn", now=300)
    case.stop("resume-turn", now=400)
    fresh = command(case, "fresh-item", turn="resume-turn")
    case.inventory([fresh], now=410)
    assert case.view()["wait"]["since"] == 410
    assert case.view()["wait"]["ever_reported"] == [fresh["identity"]]
    assert not case.control.event(original, {"type": "inventory", "certainty": "known", "workers": [finished(running)]})
