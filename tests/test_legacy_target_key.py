"""Records written before "target" was renamed "repository" still read."""
import json

from dispatcher import claims, eventlog, intents, state, waitd
from tests.test_state import make


def test_task_file_with_legacy_target_key_loads(tmp_path):
    state.save(tmp_path, make(issue=7))
    p = tmp_path / "task-portfolio_eval-7.json"
    d = json.loads(p.read_text())
    d["target"] = d.pop("repository")
    p.write_text(json.dumps(d))
    assert state.load(tmp_path, "portfolio_eval", 7).repository == "portfolio_eval"


def test_event_line_with_legacy_target_key_reads_as_repository(tmp_path):
    (tmp_path / eventlog.EVENTS_FILE).write_text(json.dumps(
        {"ts": "2026-10-01T00:00:00+00:00", "event": "claimed",
         "target": "portfolio_eval", "issue": 7}) + "\n")
    assert eventlog.read_tail(tmp_path)[0]["repository"] == "portfolio_eval"
    assert claims.last_claims(tmp_path) == {
        "portfolio_eval": "2026-10-01T00:00:00+00:00"}


def test_intent_with_legacy_target_key_keeps_its_repository(tmp_path):
    d = tmp_path / "intents"
    d.mkdir()
    (d / "1-portfolio_eval-7-reply.json").write_text(json.dumps(
        {"action": "reply", "target": "portfolio_eval", "issue": 7}))
    assert intents.list_intents(tmp_path)[0].repository == "portfolio_eval"


def test_ping_with_legacy_target_key_marks_its_repository(tmp_path):
    waitd.handle_ping(json.dumps({"issue": 7, "target": "portfolio_eval"}).encode(),
                      tmp_path)
    assert state.has_waiting(tmp_path, "portfolio_eval", 7)
