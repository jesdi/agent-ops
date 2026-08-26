import json

import pytest

from dispatcher import intents


def test_write_intent_round_trips_through_list(tmp_path):
    p = intents.write_intent(tmp_path, "reply", "portfolio_eval", 42,
                             {"text": "use oauth"},
                             actor="jesdi@github", epoch_ms=1753430000000)
    assert p == (tmp_path / "intents"
                / "1753430000000-portfolio_eval-42-reply.json")
    [it] = intents.list_intents(tmp_path)
    assert it.action == "reply"
    assert it.target == "portfolio_eval"
    assert it.issue == 42
    assert it.payload == {"text": "use oauth"}
    assert it.actor == "jesdi@github"
    assert it.created_at  # iso8601 stamped at write time
    assert it.path == p


def test_write_intent_file_body_schema(tmp_path):
    p = intents.write_intent(tmp_path, "kill", "portfolio_eval", 7, {}, "op", 1)
    d = json.loads(p.read_text())
    assert set(d) == {"action", "target", "issue", "payload", "actor",
                      "created_at"}
    assert d["action"] == "kill" and d["target"] == "portfolio_eval"
    assert d["issue"] == 7 and d["payload"] == {}


def test_write_intent_accepts_cancel_action(tmp_path):
    p = intents.write_intent(tmp_path, "cancel", 7, {}, "op", 1)
    [it] = intents.list_intents(tmp_path)
    assert it.action == "cancel" and it.path == p


def test_write_intent_rejects_unknown_action(tmp_path):
    with pytest.raises(ValueError):
        intents.write_intent(tmp_path, "explode", "portfolio_eval", 42, {},
                             "op", 1)
    assert not (tmp_path / "intents").exists()


def test_list_intents_lexical_filename_order(tmp_path):
    intents.write_intent(tmp_path, "resume", "portfolio_eval", 9, {}, "op", 200)
    intents.write_intent(tmp_path, "reply", "portfolio_eval", 8, {"text": "x"},
                         "op", 100)
    intents.write_intent(tmp_path, "park", "portfolio_eval", 7, {}, "op", 150)
    assert [i.issue for i in intents.list_intents(tmp_path)] == [8, 7, 9]


def test_list_intents_empty_when_dir_missing(tmp_path):
    assert intents.list_intents(tmp_path) == []


def test_list_intents_deletes_malformed_files_with_warning(tmp_path, capsys):
    intents.write_intent(tmp_path, "reply", "portfolio_eval", 42,
                         {"text": "hi"}, "op", 2)
    bad = tmp_path / "intents" / "1-99-reply.json"
    bad.write_text("{not json")
    missing_keys = tmp_path / "intents" / "3-98-reply.json"
    missing_keys.write_text(json.dumps({"action": "reply"}))
    got = intents.list_intents(tmp_path)
    assert [i.issue for i in got] == [42]
    assert not bad.exists() and not missing_keys.exists()
    err = capsys.readouterr().err
    assert "malformed intent" in err


def test_delete_intent_removes_file_and_is_idempotent(tmp_path):
    intents.write_intent(tmp_path, "retry", "portfolio_eval", 42, {}, "op", 5)
    [it] = intents.list_intents(tmp_path)
    intents.delete_intent(it)
    assert intents.list_intents(tmp_path) == []
    intents.delete_intent(it)  # second delete must not raise


def test_intent_roundtrip_carries_target(tmp_path):
    intents.write_intent(tmp_path, "kill", "agent_ops", 7, {}, "op", 1000)
    [i] = intents.list_intents(tmp_path)
    assert (i.target, i.issue, i.action) == ("agent_ops", 7, "kill")


def test_legacy_intent_without_target_still_listed(tmp_path):
    d = tmp_path / "intents"; d.mkdir()
    (d / "999-7-kill.json").write_text(json.dumps(
        {"action": "kill", "issue": 7, "payload": {}, "actor": "op",
         "created_at": "2026-08-26T00:00:00+00:00"}))
    [i] = intents.list_intents(tmp_path)
    assert (i.target, i.issue) == ("", 7)
