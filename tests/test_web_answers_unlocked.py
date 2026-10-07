"""Unlocked ticket 04 cases of the answers on the request route."""
from tests.test_web_answers_acceptance import REQ, answers_req, pending, rig, saved
from tests.webfakes import HEADERS


def test_large_saved_file_still_comes_back(tmp_path):
    big = {"format.note": "x" * (70 * 1024)}
    _, client = rig(tmp_path, answers_req(), saved(big))
    assert client.get(REQ, headers=HEADERS).json()["answers"] == big


def test_a_pending_set_replaces_the_file_so_a_cleared_note_stays_cleared(tmp_path):
    fake, client = rig(tmp_path, answers_req(),
                       saved({"format": "a", "format.note": "old"}))
    fake.pending = [pending({"answers": {"format": "a"}, "submit": None,
                             "revision": "r1"})]
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {"format": "a"}


def test_a_pending_submission_beats_a_newer_draft(tmp_path):
    fake, client = rig(tmp_path, answers_req())
    sub = pending({"answers": {"format": "a"}, "submit": "approve", "revision": "r1"})
    draft = pending({"answers": {"format": "b"}, "submit": None, "revision": "r1"})
    draft["created_at"] = "2026-07-25T10:00:09+00:00"
    fake.pending = [sub, draft]
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {"format": "a"}


def test_a_pending_intent_with_an_unknown_submit_is_ignored(tmp_path):
    fake, client = rig(tmp_path, answers_req(), saved({"format": "b"}))
    fake.pending = [pending({"answers": {"format": "x"}, "submit": "merge",
                             "revision": "r1"})]
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {"format": "b"}


def test_a_file_off_the_schema_gives_empty_answers(tmp_path):
    _, client = rig(tmp_path, answers_req(), saved({"format": {"x": 1}}))
    r = client.get(REQ, headers=HEADERS)
    assert r.status_code == 200 and r.json()["answers"] == {}


def test_a_pending_intent_on_another_revision_never_restores(tmp_path):
    fake, client = rig(tmp_path, answers_req(), saved({"format": "b"}))
    fake.pending = [pending({"answers": {"format": "old"}, "submit": "approve",
                             "revision": "r0"})]
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {"format": "b"}
