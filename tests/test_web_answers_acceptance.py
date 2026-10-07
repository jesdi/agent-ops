"""Ticket 04 acceptance: the answers route and the answers on the request route."""
import json

import pytest
from fastapi.testclient import TestClient

from dispatcher.state import AnswersRequest, PlanApprovalRequest
from tests.webfakes import FakeSources, HEADERS, make_config, make_task
from web.app import create_app

OK = {"answers": {"format": "a", "limit": "b", "track": "standard"},
      "submit": None, "revision": "r1"}
URL = "/api/task/alpha/7/answers"
REQ = "/api/task/alpha/7/request"


def rig(tmp_path, request=None, file=None, name=".agent/questionnaire-answers.json"):
    fake = FakeSources()
    wt = tmp_path / "wt"
    (wt / ".agent").mkdir(parents=True)
    if file is not None:
        (wt / name).write_text(file)
    fake.tasks_list = [make_task(issue=7, worktree=str(wt), operator_request=request)]
    return fake, TestClient(create_app(make_config(tmp_path), fake))


def answers_req(fp="fp-a"):
    return AnswersRequest(path=".agent/questionnaire.md", fingerprint=fp)


def pending(payload, target="alpha", issue=7, action="answers"):
    return {"action": action, "target": target, "issue": issue,
            "actor": "jesdi@github", "created_at": "2026-07-25T10:00:00+00:00",
            "id": "x.json", "text": "", "payload": payload}


def saved(answers):
    return json.dumps({"v": 1, "stage": "spec", "submitted": False,
                       "submitted_at": None, "actor": "jesdi@github",
                       "revision": "r1", "answers": answers})


def test_valid_body_returns_202_and_writes_one_intent(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post(URL, headers=HEADERS, json=OK)
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "pending",
                        "intent": "1753430000000-alpha-7-answers.json"}
    assert fake.intents == [("answers", "alpha", 7, OK, "jesdi@github")]


def test_oversized_body_is_422_and_writes_nothing(tmp_path):
    fake, client = rig(tmp_path)
    body = {"answers": {"format.note": "x" * (70 * 1024)}, "submit": None,
            "revision": "r1"}
    assert client.post(URL, headers=HEADERS, json=body).status_code == 422
    assert fake.intents == []


BAD = {
    "nested": {"answers": {"format": {"x": 1}}, "revision": "r1"},
    "upper-key": {"answers": {"Format!": "a"}, "revision": "r1"},
    "long-key": {"answers": {"a" * 65: "a"}, "revision": "r1"},
    "submit-yes": {"answers": {}, "submit": "yes", "revision": "r1"},
    "empty-revision": {"answers": {}, "revision": ""},
    "missing-revision": {"answers": {}},
    "list-with-number": {"answers": {"format": ["a", 1]}, "revision": "r1"},
    "bad-note-base": {"answers": {"x!.note": "a"}, "revision": "r1"},
}


@pytest.mark.parametrize("body", BAD.values(), ids=BAD.keys())
def test_invalid_bodies_are_422_and_write_nothing(tmp_path, body):
    fake, client = rig(tmp_path)
    assert client.post(URL, headers=HEADERS, json=body).status_code == 422
    assert fake.intents == []


@pytest.mark.parametrize("answers", [
    {"format": "a"}, {"format": ["a", "b"]}, {"format": True},
    {"format.note": "why"}, {"track": "standard"}, {}],
    ids=["string", "list", "bool", "note", "track", "empty"])
@pytest.mark.parametrize("submit", [None, "changes", "approve"])
def test_valid_values_pass(tmp_path, answers, submit):
    fake, client = rig(tmp_path)
    body = {"answers": answers, "submit": submit, "revision": "r1"}
    assert client.post(URL, headers=HEADERS, json=body).status_code == 202
    assert len(fake.intents) == 1


@pytest.mark.parametrize("request_", [
    PlanApprovalRequest(path=".agent/review.html", fingerprint="fp-plan"),
    AnswersRequest(path=".agent/questionnaire.md", fingerprint="fp-ans")],
    ids=["plan-approval", "answers"])
def test_request_route_returns_the_fingerprint_as_revision(tmp_path, request_):
    _, client = rig(tmp_path, request_)
    body = client.get(REQ, headers=HEADERS).json()
    assert body["revision"] == request_.fingerprint


def test_pending_intent_is_overlaid_on_the_saved_file(tmp_path):
    fake, client = rig(tmp_path, answers_req(), saved({"format": "b"}))
    fake.pending = [pending({"answers": {"format": "b", "limit": "a"},
                             "submit": None, "revision": "r1"})]
    body = client.get(REQ, headers=HEADERS).json()
    assert body["answers"] == {"format": "b", "limit": "a"}


def test_saved_file_alone_is_returned(tmp_path):
    _, client = rig(tmp_path, answers_req(), saved({"format": "b"}))
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {"format": "b"}


def test_saved_review_answers_file_for_a_plan_approval_request(tmp_path):
    _, client = rig(tmp_path,
                    PlanApprovalRequest(path=".agent/review.html", fingerprint="f"),
                    saved({"track": "fast"}), ".agent/review-answers.json")
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {"track": "fast"}


def test_newest_pending_intent_wins(tmp_path):
    fake, client = rig(tmp_path, answers_req())
    fake.pending = [pending({"answers": {"format": "a"}, "submit": None, "revision": "r1"}),
                    pending({"answers": {"format": "b"}, "submit": None, "revision": "r1"})]
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {"format": "b"}


def test_no_file_and_no_intent_gives_empty_answers(tmp_path):
    _, client = rig(tmp_path, answers_req())
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {}


def test_garbage_file_gives_empty_answers(tmp_path):
    _, client = rig(tmp_path, answers_req(), "{not json")
    r = client.get(REQ, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["answers"] == {}


@pytest.mark.parametrize("other", [
    pending({"answers": {"limit": "z"}}, target="beta"),
    pending({"answers": {"limit": "z"}}, issue=8),
    pending({"text": "hi"}, action="reply"),
], ids=["other-target", "other-issue", "other-action"])
def test_other_pending_intents_do_not_leak(tmp_path, other):
    fake, client = rig(tmp_path, answers_req(), saved({"format": "b"}))
    fake.pending = [other]
    assert client.get(REQ, headers=HEADERS).json()["answers"] == {"format": "b"}


def test_unknown_task_is_404_on_both_routes(tmp_path):
    _, client = rig(tmp_path)
    assert client.post(URL, headers=HEADERS, json=OK).status_code == 202  # route exists
    assert client.get("/api/task/alpha/999/request", headers=HEADERS).status_code == 404
    assert client.post("/api/task/alpha/999/answers", headers=HEADERS,
                       json=OK).status_code == 404


def test_missing_login_header_is_401_on_both_routes(tmp_path):
    fake, client = rig(tmp_path, answers_req())
    assert client.get(REQ).status_code == 401
    assert client.post(URL, json=OK).status_code == 401
    assert client.post(URL, headers=HEADERS, json=OK).status_code == 202  # route exists
    fake.intents.clear()
    assert fake.intents == []


def test_openapi_lists_the_route_and_the_new_fields(tmp_path):
    schema = create_app(make_config(tmp_path), FakeSources()).openapi()
    assert "post" in schema["paths"][URL.replace("alpha", "{target}").replace("7", "{issue}")]
    props = schema["components"]["schemas"]["OperatorRequest"]["properties"]
    assert "revision" in props and "answers" in props
