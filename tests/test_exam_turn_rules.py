"""The rules over turns: tip cap, one turn at a time, assisted, flags sealed at submit, chat after submit,
the right attempt, and a head change. Through `POST .../turns`, `POST .../answers`, `GET .../`
and `exams.work`, on top of test_exam_turns.py.

Interface under test (defined here, built by the ticket):
    POST .../turns   409 for a fourth tip on a question of the attempt (a running tip counts, a failed one
        does not), for a tip once the attempt is submitted, for a second turn while one is running on the
        same question or in the general chat, and for a head_sha that is not the exam's. 409 runs no model
        command and stores nothing. A question or general turn is accepted for any kept attempt of the
        head: the open one, the one grading, a passed one, a failed one (`attempt` names it).
    GET .../ the questions of every attempt carry `tips`, `assisted` and `thread` like the open one's do:
        `exam.questions[]` for the open attempt, `attempts[].questions[]` for a submitted one.
    pr_exam.context(attempt_dir, turn): `attempt_dir` is the attempt the turn names, never a later one.
    The flags a submit writes to `attempts/<n>/flags.json` are {"bypassed": [id], "assisted": [id]}: the
        questions with a finished tip or question-thread reply while the attempt was open. A turn that
        finishes later changes neither the file nor the scoring.
    A turn that finishes after a new head replaced the exam is dropped: its stream ends "failed" and the
        new attempt keeps no turn.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from dispatcher import exams
from tests.test_exam_generation import GET, HEAD, REPO
from tests.test_exam_grading import Session, answers_for, graded_once, opened, result_of, submit
from tests.test_exam_question_bypass import GRADES, SIX, Flagging, without
from tests.test_exam_turns import (
    REPLY, Chat, RealChat, Turns, first_delta, follow, question, stored_turns, thread_of)

NEW = "def456"


class Assist(Chat, Flagging):
    """Chat's context, and Flagging's score that also marks the `assisted` flags like `score --flags`."""
    real = False

    def __init__(self):
        Chat.__init__(self)
        self.seen, self.recorded = [], []

    def context(self, attempt_dir, turn):
        if self.real:
            self.asked.append({"attempt_dir": Path(attempt_dir), "turn": turn})
            return RealChat.context(self, attempt_dir, turn)
        return Chat.context(self, attempt_dir, turn)

    def score(self, exam, answers, grades, flags=None):
        out = Flagging.score(self, exam, answers, grades, flags)
        for q in out["questions"]:
            if q["id"] in self.seen[-1].get("assisted", []):
                q["help"] = "assisted"
        return out

    def record(self, records, exam, result, today):
        self.recorded.append(json.loads(Path(result).read_text()))


class Rules(Turns):
    PR_EXAM = Assist


class RealRules(Turns):
    class PR_EXAM(Assist):
        real = True


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Rules(tmp_path, monkeypatch)
    yield b
    b.release()


@pytest.fixture
def real(tmp_path, monkeypatch):
    b = RealRules(tmp_path, monkeypatch)
    yield b
    b.release()


def questions_of(view, attempt):
    if view.get("exam") and view["attempt"] == attempt:
        return view["exam"]["questions"]
    return next(a for a in view["attempts"] if a["attempt"] == attempt)["questions"]


async def settle_at(box, c, attempt, qid):
    for _ in range(250):
        thread = next(q for q in questions_of(await box.view(c), attempt) if q["id"] == qid)["thread"]
        if thread and thread[-1]["status"] != "running":
            return thread
        await asyncio.sleep(0.02)
    raise AssertionError("the turn never left running")


def runs(box):
    return len(list(box.ctl.glob("run-*")))


def attempt_files(box, name):
    return sorted(Path(box.cfg.state_dir).glob(f"exams/**/attempts/*/{name}"))


def test_a_fourth_tip_is_refused_and_runs_no_model_command(box):
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            for n in range(3):
                await box.tip(c, body, message=f"tip {n}")
            fourth = await box.ask(c, body, "tip", "q5", "tip 3")
            return fourth, await box.view(c)

    fourth, view = asyncio.run(go())
    assert fourth.status_code == 409
    assert (len(box.cmds), runs(box)) == (3, 3)
    q5 = question(view, "q5")
    assert (q5["tips"], len(q5["thread"])) == (3, 3)
    assert question(view, "q2")["tips"] == 0


def test_two_tip_requests_sent_at_the_same_moment_accept_one_and_refuse_the_other(box):
    body = opened(box)
    box.hold()

    async def go():
        async with box.aclient() as c:
            both = await asyncio.gather(box.ask(c, body, "tip", "q5", "one"), box.ask(c, body, "tip", "q5", "two"))
            box.release()
            await box.settle(c)
            return both, await box.view(c)

    both, view = asyncio.run(go())
    assert sorted(r.status_code for r in both) == [202, 409]
    assert (len(box.cmds), runs(box)) == (1, 1)
    q5 = question(view, "q5")
    assert (q5["tips"], len(q5["thread"])) == (1, 1)


def test_a_running_tip_holds_its_slot_and_the_general_chat_has_its_own_turn(box):
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            await box.tip(c, body)
            await box.tip(c, body)
            box.hold()
            third = await box.ask(c, body, "tip", "q5", "third")
            again = await box.ask(c, body, "tip", "q5", "fourth while the third runs")
            thread = await box.ask(c, body, "question", "q5", "asked while a tip runs")
            other = await box.ask(c, body, "question", "q2", "another question")
            chat = await box.ask(c, body, "general", None, "overview?")
            chat_again = await box.ask(c, body, "general", None, "overview again?")
            return [r.status_code for r in (third, again, thread, other, chat, chat_again)]

    assert asyncio.run(go()) == [202, 409, 409, 202, 202, 409]


def test_a_tip_that_fails_mid_stream_leaves_the_count_and_assisted_state_and_gives_its_slot_back(box):
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            await box.tip(c, body)
            before = question(await box.view(c), "q5")
            (box.ctl / "fail").write_text("")
            r = await box.ask(c, body, "tip", "q5", "doomed")
            assert r.status_code == 202, r.text
            failed = await box.settle(c)
            after = question(await box.view(c), "q5")
            (box.ctl / "fail").unlink()
            await box.tip(c, body, message="two")
            await box.tip(c, body, message="three")
            fourth = await box.ask(c, body, "tip", "q5", "four")
            return before, failed, after, question(await box.view(c), "q5"), fourth

    before, failed, after, last, fourth = asyncio.run(go())
    assert failed[-1]["status"] == "failed"
    assert (before["tips"], before["assisted"]) == (1, True)
    assert (after["tips"], after["assisted"]) == (1, True)
    assert (last["tips"], fourth.status_code) == (3, 409)


def test_a_reply_in_a_question_thread_makes_that_question_assisted(box):
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            before = await box.view(c)
            r = await box.ask(c, body, "question", "q2", "is option 1 right?")
            assert r.status_code == 202, r.text
            await box.settle(c, "q2")
            return before, await box.view(c)

    before, view = asyncio.run(go())
    assert [q["assisted"] for q in before["exam"]["questions"]] == [False] * 6
    assert [q["assisted"] for q in view["exam"]["questions"]] == [False, True, False, False, False, False]
    assert question(view, "q2")["tips"] == 0


def test_a_reply_in_the_general_chat_makes_no_question_assisted(box):
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "general", None, "what is the riskiest change?")
            assert r.status_code == 202, r.text
            for _ in range(250):
                if len(stored_turns(box)) == 1 and stored_turns(box)[0]["status"] != "running":
                    break
                await asyncio.sleep(0.02)
            return await box.view(c)

    view = asyncio.run(go())
    assert [t["status"] for t in stored_turns(box)] == ["done"]
    assert [q["assisted"] for q in view["exam"]["questions"]] == [False] * 6


def submitted_with_q2_assisted_and_q6_bypassed(box, body, c):
    async def go():
        r = await box.ask(c, body, "question", "q2", "is option 1 right?")
        assert r.status_code == 202, r.text
        await box.settle(c, "q2")
        r = await c.post(f"{GET}/bypass", json={"attempt": 1, "head_sha": HEAD, "question": "q6"})
        assert r.status_code == 200, r.text
        r = await c.post(f"{GET}/answers", json={"attempt": 1, "head_sha": HEAD,
                                                 "answers": without(answers_for(body), "q6")})
        assert r.status_code == 202, r.text
    return go()


def test_a_reply_that_finishes_while_grading_is_stored_but_changes_neither_flags_nor_score(box):
    body = opened(box)
    box.hold()

    async def go():
        async with box.aclient() as c:
            box.release()
            await submitted_with_q2_assisted_and_q6_bypassed(box, body, c)
            box.hold()
            (box.ctl / "release").unlink()
            r = await box.ask(c, body, "question", "q5", "is the token random?")
            assert r.status_code == 202, r.text
            _, q, _, wait = await box.stream(r.json()["id"])
            await first_delta(q)
            box.release()
            _, final = await follow(q)
            await wait()
            return final, await settle_at(box, c, 1, "q5")

    final, thread = asyncio.run(go())
    assert final == {"status": "done"}
    assert [(t["message"], t["text"], t["status"]) for t in thread] == [("is the token random?", REPLY, "done")]
    [flags] = attempt_files(box, "flags.json")
    assert json.loads(flags.read_text()) == {"bypassed": ["q6"], "assisted": ["q2"]}
    box.work()
    assert box.pr_exam.seen == [{"bypassed": ["q6"], "assisted": ["q2"]}]
    [recorded] = box.pr_exam.recorded
    helps = {q["id"]: q.get("help") for q in recorded["questions"]}
    assert helps == {"q1": None, "q2": "assisted", "q3": None, "q4": None, "q5": None, "q6": "bypassed"}


def test_a_tip_is_refused_while_grading_and_the_attempt_stays_as_submitted(box):
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            await submit_all(box, body, c)
            tip = await box.ask(c, body, "tip", "q5", "too late")
            return tip, await box.view(c)

    tip, view = asyncio.run(go())
    assert tip.status_code == 409
    assert (box.cmds, runs(box), stored_turns(box)) == ([], 0, [])
    assert view["state"] == "grading"


async def submit_all(box, body, c):
    r = await c.post(f"{GET}/answers", json={"attempt": 1, "head_sha": HEAD, "answers": answers_for(body)})
    assert r.status_code == 202, r.text


def test_a_message_in_a_question_thread_while_grading_streams_with_the_submitted_answer_and_no_points(real):
    body = opened(real)

    async def go():
        async with real.aclient() as c:
            await submit_all(real, body, c)
            r = await real.ask(c, body, "question", "q5", "is my answer right?")
            assert r.status_code == 202, r.text
            return await settle_at(real, c, 1, "q5")

    thread = asyncio.run(go())
    assert [(t["status"], t["text"]) for t in thread] == [("done", REPLY)]
    prompt = real.stdin(0)
    assert "ANSWER-q5" in prompt and "is my answer right?" in prompt
    assert "Points" not in prompt and "Feedback" not in prompt


FEEDBACK = "misses that the token only grows"


def failed_first_attempt(real):
    """Attempt 1 fails with q5 answered "the token is random", 0 points and the feedback; attempt 2 is open."""
    real.session.grades = {"q4": "pass", "q5": "fail", "q6": "pass"}
    real.session.followup_spec = [("q5", "rubric-fixture", "text"), ("q1", "lease-ownership", "choice")]
    write = real.session.__call__

    def with_feedback(request, *, model, timeout):
        write(request, model=model, timeout=timeout)
        req = json.loads(Path(request).read_text())
        if req["mode"] == "grade":
            out = Path(req["out"])
            doc = json.loads(out.read_text())
            doc["q5"]["feedback"] = FEEDBACK
            out.write_text(json.dumps(doc))
    real.session = with_feedback
    first = opened(real)
    return first, graded_once(real, first, answers=answers_for(first, wrong=("q1",), q5="the token is random"))


def test_a_question_asked_about_a_failed_attempt_is_built_from_that_attempt_and_marks_nothing(real):
    first, second = failed_first_attempt(real)
    assert (second["state"], second["attempt"]) == ("open", 2)

    async def go():
        async with real.aclient() as c:
            r = await real.ask(c, first, "question", "q5", "why was I wrong?")
            assert r.status_code == 202, r.text
            return await settle_at(real, c, 1, "q5"), await real.view(c)

    thread, view = asyncio.run(go())
    assert [(t["message"], t["status"]) for t in thread] == [("why was I wrong?", "done")]
    [asked] = real.pr_exam.asked
    assert asked["attempt_dir"] == Path(real.cfg.state_dir) / "exams" / REPO / "240" / "attempts" / "1"
    prompt = real.stdin(0)
    assert "the token is random" in prompt and FEEDBACK in prompt and "why was I wrong?" in prompt
    assert re.search(r"Points\W{0,6}0\b", prompt), prompt
    assert [json.loads(p.read_text()) for p in attempt_files(real, "flags.json")] == [
        {"bypassed": [], "assisted": []}]
    assert [q["assisted"] for q in view["exam"]["questions"]] == [False, False]
    assert [(q["tips"], q["thread"]) for q in view["exam"]["questions"]] == [(0, []), (0, [])]
    assert all(not json.loads(p.read_text()).get("turns") for p in attempt_files(real, "threads.json")
               if p.parent.name == "2")


def test_the_grading_session_inputs_hold_no_tip_and_no_thread_message(box):
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            for n in range(2):
                await box.tip(c, body, message=f"SECRET-DRAFT-{n}")
            for n in range(3):
                r = await box.ask(c, body, "question", "q5", f"SECRET-MESSAGE-{n}")
                assert r.status_code == 202, r.text
                await box.settle(c)

    asyncio.run(go())
    assert len(stored_turns(box)) == 5
    assert graded_once(box, body)["state"] == "passed"
    [call] = box.session.graded()
    joined = json.dumps(call["req"]) + "".join(call["files"].values())
    assert set(call["req"]) == {"mode", "repo", "pr", "head_sha", "diff", "questions", "answers", "out"}
    for secret in ("SECRET-", "Part one", "Part two"):
        assert secret not in joined


def test_a_turn_still_running_when_a_new_head_restarts_the_exam_is_dropped(box):
    body = opened(box)
    box.hold()

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "tip", "q5", "a tip on the old head")
            assert r.status_code == 202, r.text
            _, q, _, wait = await box.stream(r.json()["id"])
            assert await first_delta(q) == "Part one. "
            box.github.head = NEW
            box.request()
            box.work()
            restarted = await box.view(c)
            box.release()
            _, final = await follow(q)
            await wait()
            return restarted, final, await box.view(c)

    restarted, final, view = asyncio.run(go())
    assert (restarted["head_sha"], restarted["attempt"]) == (NEW, 1)
    assert final["status"] == "failed"
    q5 = question(view, "q5")
    assert (view["head_sha"], q5["tips"], q5["assisted"], q5["thread"]) == (NEW, 0, False, [])
    assert [t for t in stored_turns(box)] == []
