"""Tips and question chat, streamed: `POST .../turns`, `GET .../turns/{id}/stream`, the console startup
and `containers.turn_cmd`, on top of test_exam_question_bypass.py.

Interface under test (defined here, built by the ticket):
    Sources(..., pr_exam=<object with context()>)   the console's `pr_exam`, as `exams.work` takes one
    pr_exam.context(exam: Path, diff: Path, body: Path, turn: dict) -> str
        `exam` is `<exam dir>/attempts/<n>/exam.json` of the turn's attempt, `diff` and `body` the
        head's. `turn` is the document `pr_exam.py context --turn` takes. Returns the prompt (what
        `pr_exam.py context` prints); it refuses a prompt that is too long by raising
        exam_runner.PromptTooLong, and refuses for another reason by raising ValueError.
    containers.turn_cmd(name, state_dir, memory, cpus, model, effort="") -> argv
        `inspection_cmd`'s sandbox without the clone and the request mounts, streaming its output:
        claude `--output-format stream-json --include-partial-messages --verbose --tools ''`,
        codex `exec --json`. The prompt arrives on stdin. The console runs that argv itself, so the
        tests swap `containers.turn_cmd` for a fake argv that reads stdin and prints what the
        runtime prints.
    POST /api/exam/{owner}/{name}/{pr}/turns
        {"attempt": int, "head_sha": str, "kind": "tip"|"question"|"general", "question": id (not for
        general), "message": str (the draft of a tip, or the operator's message)}
        202 {"id": str} at once, the reply is produced by a background task of the console.
        429 detail "unavailable: usage limit" when no `models.triage` entry is admitted, 422 detail
        "PR too large for chat" when `context` refuses the prompt as too long, 503 with the skill's
        reason when it refuses for another. All before any model call, storing nothing.
    GET .../turns/{id}/stream   text/event-stream, one `data: {"delta": str}` per chunk of the reply
        (what has been written so far arrives first, as one delta), then one
        `data: {"status": "done"}` or `data: {"status": "failed", "error": str}`.
    GET .../ `exam.questions[]` of the open attempt also carries `tips` (finished tips), `tips_left`
        (the cap less the finished and running tips), `assisted` (bool) and `thread`: [{"id", "kind",
        "message", "text", "status": "running"|"done"|"failed", "error"}], oldest first.
    The console's startup marks every `running` turn failed, with an error.
"""
import asyncio
import contextlib
import json
import sys
from pathlib import Path

import httpx
import pytest

from dispatcher import containers, runtimes
from dispatcher.exam_runner import PrExam, PromptTooLong
from tests.test_exam_debt import Gh
from tests.test_exam_generation import GET, HEADERS, NOW, REPO
from tests.test_exam_grading import answers_for, opened, submit
from tests.test_exam_question_bypass import GRADES, SIX
from tests.test_exam_records import Box as RecordingBox, Recording
from tests.test_web_sse import _drive_sse
from tests.usagefakes import session_usage
from web.app import create_app
from web.sources import Sources

ONE, TWO = "Part one. ", "Part two."
REPLY = ONE + TWO

MODEL = '''
import json, pathlib, sys, time
runtime, ctl = sys.argv[1], pathlib.Path(sys.argv[2])
prompt = sys.stdin.read()
(ctl / f"run-{len(list(ctl.glob('run-*')))}").write_text(prompt)
if (ctl / "empty").exists():
    sys.exit(0)

def emit(text):   # Codex's exec --json has no deltas: it sends the whole message at the end
    if runtime == "claude":
        print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": text}}}), flush=True)

emit("%s")
if (ctl / "hold").exists():
    for _ in range(1000):
        if (ctl / "release").exists():
            break
        time.sleep(0.02)
if (ctl / "fail").exists():
    print("boom", file=sys.stderr, flush=True)
    sys.exit(3)
emit("%s")
if runtime == "claude":
    print(json.dumps({"type": "result", "subtype": "success", "result": "%s"}), flush=True)
else:
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "i", "type": "agent_message", "text": "%s"}}), flush=True)
    print(json.dumps({"type": "turn.completed"}), flush=True)
''' % (ONE, TWO, ONE + TWO, ONE + TWO)


class Chat(Recording):
    def __init__(self):
        super().__init__()
        self.asked, self.refusal = [], None

    def context(self, exam, diff, body, turn):
        if self.refusal:
            raise self.refusal
        self.asked.append({"exam": Path(exam), "turn": turn})
        return f"CONTEXT {turn['kind']} {turn.get('question')} :: {turn.get('message', turn.get('draft', ''))}"


class Turns(RecordingBox):
    PR_EXAM = Chat

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch, spec=SIX, grades=GRADES)
        self.ctl = tmp_path / "ctl"
        self.ctl.mkdir()
        self.script = tmp_path / "model.py"
        self.script.write_text(MODEL)
        self.cmds = []
        monkeypatch.setattr(containers, "turn_cmd", self._turn_cmd, raising=False)
        self.app = self.make_app()

    def _turn_cmd(self, name, state_dir, memory, cpus, model, effort=""):
        self.cmds.append({"name": name, "state_dir": state_dir, "model": model})
        return [sys.executable, str(self.script), "codex" if "gpt" in model else "claude", str(self.ctl)]

    def make_app(self):
        box = self

        class Src(Sources):
            def usage(self, routed):
                return box.usages

        return create_app(self.cfg, Src(self.cfg.state_dir, None, None, clock=lambda: 1000.0, pr_exam=self.pr_exam),
                          sse_interval=0.01, clock=lambda: box.now)

    def hold(self):
        (self.ctl / "hold").write_text("")

    def release(self):
        (self.ctl / "release").write_text("")

    def stdin(self, n):
        return (self.ctl / f"run-{n}").read_text()

    def aclient(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver",
                                 headers=HEADERS)

    async def ask(self, c, body, kind, question=None, message="", **over):
        doc = {"attempt": body["attempt"], "head_sha": body["head_sha"], "kind": kind, "message": message}
        if question:
            doc["question"] = question
        return await c.post(f"{GET}/turns", json={**doc, **over})

    async def view(self, c):
        r = await c.get(GET)
        assert r.status_code == 200, r.text
        return r.json()

    async def stream(self, turn_id):
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "path": f"{GET}/turns/{turn_id}/stream", "raw_path": f"{GET}/turns/{turn_id}/stream".encode(),
                 "query_string": b"", "root_path": "", "headers": [(b"tailscale-user-login", b"jesdi@github")],
                 "server": ("testserver", 80), "client": ("testclient", 50000)}
        return await _drive_sse(self.app, scope)

    async def settle(self, c, question="q5"):
        for _ in range(250):
            thread = thread_of(await self.view(c), question)
            if thread and thread[-1]["status"] != "running":
                return thread
            await asyncio.sleep(0.02)
        raise AssertionError("the turn never left running")

    async def tip(self, c, body, question="q5", message="a tip"):
        """A whole tip, run to its end."""
        r = await self.ask(c, body, "tip", question, message)
        assert r.status_code == 202, r.text
        return await self.settle(c, question)


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Turns(tmp_path, monkeypatch)
    yield b
    b.release()


def thread_of(view, qid):
    return next(q for q in view["exam"]["questions"] if q["id"] == qid)["thread"]


def question(view, qid):
    return next(q for q in view["exam"]["questions"] if q["id"] == qid)


async def follow(q, final=("done", "failed")):
    chunks = []
    while True:
        line = await asyncio.wait_for(q.get(), timeout=5)
        if not line.startswith("data:"):
            continue
        doc = json.loads(line.removeprefix("data:").strip())
        if "delta" in doc:
            chunks.append(doc["delta"])
        elif doc.get("status") in final:
            return chunks, doc


async def first_delta(q):
    while True:
        line = await asyncio.wait_for(q.get(), timeout=5)
        if line.startswith("data:") and "delta" in json.loads(line.removeprefix("data:").strip()):
            return json.loads(line.removeprefix("data:").strip())["delta"]


@contextlib.asynccontextmanager
async def started(app):
    """The app's lifespan: startup now, shutdown on exit."""
    inbox, outbox = asyncio.Queue(), asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})
    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, inbox.get, outbox.put))
    message = await asyncio.wait_for(outbox.get(), 5)
    assert message["type"] == "lifespan.startup.complete", message
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(outbox.get(), 5)
        await task


def stored_turns(box):
    return [t for p in (Path(box.cfg.state_dir) / "exams").rglob("threads.json")
            for t in json.loads(p.read_text()).get("turns", [])]


def test_a_tip_streams_in_more_than_one_chunk_and_is_there_after_a_reload(box):
    body = opened(box)
    box.hold()

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "tip", "q5", "the token only grows")
            assert r.status_code == 202, r.text
            _, q, _, wait = await box.stream(r.json()["id"])
            assert await first_delta(q) == ONE
            box.release()
            chunks, final = await follow(q)
            await wait()
            return chunks, final, await box.view(c)

    chunks, final, view = asyncio.run(go())
    assert final == {"status": "done"} and chunks == [TWO]
    q5 = question(view, "q5")
    assert (q5["tips"], q5["assisted"]) == (1, True)
    [turn] = q5["thread"]
    assert (turn["kind"], turn["message"], turn["text"], turn["status"]) == ("tip", "the token only grows", REPLY, "done")
    assert box.stdin(0) == "CONTEXT tip q5 :: the token only grows"
    [asked] = box.pr_exam.asked
    assert asked["exam"] == Path(box.cfg.state_dir) / "exams" / REPO / "240" / "attempts" / str(body["attempt"]) / "exam.json"
    assert question(box.read().json(), "q5")["thread"] == q5["thread"]


def test_a_message_in_a_question_thread_streams_under_that_question_and_survives_a_reload(box):
    body = opened(box)
    box.hold()

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "question", "q2", "is option 1 right?")
            assert r.status_code == 202, r.text
            _, q, _, wait = await box.stream(r.json()["id"])
            assert await first_delta(q) == ONE
            box.release()
            chunks, final = await follow(q)
            await wait()
            return chunks, final

    chunks, final = asyncio.run(go())
    assert final == {"status": "done"} and chunks == [TWO]
    view = box.read().json()
    [turn] = thread_of(view, "q2")
    assert (turn["kind"], turn["message"], turn["text"]) == ("question", "is option 1 right?", REPLY)
    assert thread_of(view, "q5") == []
    assert box.stdin(0) == "CONTEXT question q2 :: is option 1 right?"


def test_closing_the_page_mid_reply_still_stores_the_whole_tip(box):
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            await box.tip(c, body)
            box.hold()
            r = await box.ask(c, body, "tip", "q5", "tip two")
            assert r.status_code == 202, r.text
            _, q, disconnect, wait = await box.stream(r.json()["id"])
            assert await first_delta(q) == ONE
            disconnect()
            await wait()
            box.release()
            await box.settle(c)
            return await box.view(c)

    q5 = question(asyncio.run(go()), "q5")
    assert (q5["tips"], q5["assisted"]) == (2, True)
    assert [(t["message"], t["text"], t["status"]) for t in q5["thread"]] == [
        ("a tip", REPLY, "done"), ("tip two", REPLY, "done")]
    assert question(box.read().json(), "q5")["thread"] == q5["thread"]


@pytest.mark.parametrize("model", ["anthropic/claude-opus-5", "openai/gpt-5"])
def test_the_turn_command_is_the_sandbox_of_an_inspection_without_a_clone_or_tools(monkeypatch, model):
    monkeypatch.setattr(containers, "_host_binary",
                        lambda runtime: ["-v", f"/cli:/usr/local/bin/{runtime.cli}:ro"])
    argv = containers.turn_cmd("turn-a", "/state", "1g", "1", model)
    runtime = runtimes.runtime_for(model)
    mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-v"]
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert "--rm" in argv and "-i" in argv
    assert argv[argv.index("--tmpfs") + 1].startswith(f"{runtime.mount}:rw,nosuid,nodev")
    auth = f"/state/{runtime.home}/{runtime.auth_file}:{runtime.mount}/{runtime.auth_file}:ro"
    assert sorted(mounts) == sorted([auth, f"/cli:/usr/local/bin/{runtime.cli}:ro"])
    assert not any("/repository" in part for part in argv)
    if model.startswith("anthropic"):
        assert "-p" in argv
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--include-partial-messages" in argv and "--verbose" in argv
    else:
        assert "exec" in argv and "--json" in argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_a_codex_entry_runs_codex_and_its_whole_reply_streams_as_one_chunk(box):
    body = opened(box)
    box.usages = {"anthropic": session_usage(util=1.0, provider="anthropic", now=NOW),
                  "openai": session_usage(provider="openai", now=NOW)}

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "tip", "q5", "the token only grows")
            assert r.status_code == 202, r.text
            _, q, _, wait = await box.stream(r.json()["id"])
            chunks, final = await follow(q)
            await wait()
            return chunks, final, await box.view(c)

    chunks, final, view = asyncio.run(go())
    assert [c["model"] for c in box.cmds] == [box.cmds[0]["model"]] and "gpt-5" in box.cmds[0]["model"]
    assert chunks == [ONE + TWO] and final == {"status": "done"}
    [turn] = question(view, "q5")["thread"]
    assert ONE.strip() in turn["text"] and TWO in turn["text"] and turn["status"] == "done"
    assert box.stdin(0) == "CONTEXT tip q5 :: the token only grows"


def test_usage_that_admits_no_entry_refuses_a_tip_at_once_and_stores_nothing(box):
    body = opened(box)
    box.usages = {p: session_usage(util=1.0, provider=p, now=NOW) for p in ("anthropic", "openai")}

    async def go():
        async with box.aclient() as c:
            return await box.ask(c, body, "tip", "q5", "hint"), await box.view(c)

    r, view = asyncio.run(go())
    assert r.status_code == 429 and r.json()["detail"] == "unavailable: usage limit"
    assert box.cmds == [] and list((box.ctl).glob("run-*")) == []
    assert (question(view, "q5")["tips"], question(view, "q5")["thread"]) == (0, [])
    assert stored_turns(box) == []


@pytest.mark.parametrize("kind, qid", [("tip", "q5"), ("question", "q2"), ("general", None)])
def test_a_prompt_that_is_too_long_is_refused_before_any_model_call_and_stores_nothing(box, kind, qid):
    body = opened(box)
    box.pr_exam.refusal = PromptTooLong("prompt is 500,000 characters, over the 400,000-character limit")

    async def go():
        async with box.aclient() as c:
            return await box.ask(c, body, kind, qid, "hello"), await box.view(c)

    r, view = asyncio.run(go())
    assert r.status_code == 422 and r.json()["detail"] == "PR too large for chat"
    assert box.cmds == [] and list(box.ctl.glob("run-*")) == []
    assert (question(view, "q5")["tips"], thread_of(view, "q5"), thread_of(view, "q2")) == (0, [], [])
    assert stored_turns(box) == []


@pytest.mark.parametrize("refusal", [ValueError("question q9: not in the exam"), OSError("python is gone")])
def test_a_prompt_the_skill_cannot_build_says_why_before_any_model_call_and_stores_nothing(box, refusal):
    body = opened(box)
    box.pr_exam.refusal = refusal

    async def go():
        async with box.aclient() as c:
            return await box.ask(c, body, "tip", "q5", "hello")

    r = asyncio.run(go())
    assert r.status_code == 503 and str(refusal) in r.json()["detail"]
    assert "too large" not in r.json()["detail"]
    assert box.cmds == [] and stored_turns(box) == []


def test_an_empty_reply_fails_the_turn_and_costs_no_tip(box):
    body = opened(box)
    (box.ctl / "empty").write_text("")

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "tip", "q5", "hint")
            assert r.status_code == 202, r.text
            _, q, _, wait = await box.stream(r.json()["id"])
            final = (await follow(q))[1]
            await wait()
            return final, await box.view(c)

    final, view = asyncio.run(go())
    assert final == {"status": "failed", "error": "the model sent no reply"}
    q5 = question(view, "q5")
    assert (q5["tips"], q5["tips_left"], q5["assisted"]) == (0, 3, False)
    assert [(t["status"], t["error"]) for t in q5["thread"]] == [("failed", "the model sent no reply")]


def test_a_reply_that_cannot_be_stored_ends_its_stream_failed_and_frees_its_thread(box, monkeypatch):
    body = opened(box)
    real, calls = Sources.end_turn, []

    def flaky(self, *args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("disk full")
        return real(self, *args)
    monkeypatch.setattr(Sources, "end_turn", flaky)

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "tip", "q5", "hint")
            assert r.status_code == 202, r.text
            _, q, _, wait = await box.stream(r.json()["id"])
            final = (await follow(q))[1]
            await wait()
            failed = question(await box.view(c), "q5")
            await box.tip(c, body)
            return final, failed, question(await box.view(c), "q5")

    final, failed, after = asyncio.run(go())
    assert final["status"] == "failed" and "disk full" in final["error"]
    assert (failed["tips"], failed["assisted"], [t["status"] for t in failed["thread"]]) == (0, False, ["failed"])
    assert (after["tips"], [t["status"] for t in after["thread"]]) == (1, ["failed", "done"])


def test_a_console_restart_fails_the_running_tip_and_frees_its_slot(box):
    body = opened(box)

    async def before():
        async with box.aclient() as c:
            await box.tip(c, body)
            box.hold()
            r = await box.ask(c, body, "tip", "q5", "tip two")
            assert r.status_code == 202, r.text
            _, q, disconnect, wait = await box.stream(r.json()["id"])
            await first_delta(q)
            disconnect()
            await wait()

    asyncio.run(before())      # the loop and its turn task end here: the console went down
    box.release()

    async def after(app):
        box.app = app
        async with started(app), box.aclient() as c:
            return await box.view(c)

    first = asyncio.run(after(box.make_app()))
    q5 = question(first, "q5")
    assert (q5["tips"], q5["assisted"]) == (1, True)
    assert [t["status"] for t in q5["thread"]] == ["done", "failed"]
    assert q5["thread"][1]["error"]
    second = asyncio.run(after(box.make_app()))   # a second console starting changes nothing
    assert question(second, "q5") == q5


def test_a_turn_that_fails_mid_stream_shows_its_error_costs_no_tip_and_can_be_retried(box):
    body = opened(box)
    (box.ctl / "fail").write_text("")

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "tip", "q5", "hint")
            assert r.status_code == 202, r.text
            _, q, _, wait = await box.stream(r.json()["id"])
            chunks, final = await follow(q)
            await wait()
            failed = await box.view(c)
            (box.ctl / "fail").unlink()
            retry = await box.ask(c, body, "tip", "q5", "hint")
            assert retry.status_code == 202, retry.text
            await box.settle(c)
            return chunks, final, failed, await box.view(c)

    chunks, final, failed, again = asyncio.run(go())
    assert chunks == [ONE] and final["status"] == "failed" and final["error"]
    q5 = question(failed, "q5")
    assert (q5["tips"], q5["assisted"]) == (0, False)
    [turn] = q5["thread"]
    assert turn["status"] == "failed" and turn["error"]
    q5 = question(again, "q5")
    assert (q5["tips"], q5["assisted"]) == (1, True)
    assert [t["status"] for t in q5["thread"]] == ["failed", "done"]


def test_an_exam_deleted_after_a_merge_with_a_pass_loses_its_threads_and_keeps_its_records(box):
    box.github = Gh()
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            await box.tip(c, body, "q1")
            r = await box.ask(c, body, "question", "q2", "why?")
            assert r.status_code == 202, r.text
            await box.settle(c, "q2")

    asyncio.run(go())
    assert len(stored_turns(box)) == 2
    assert submit(box, body, answers=answers_for(body)).status_code == 202
    box.work()
    assert box.read().json()["state"] == "passed"
    records = box.records(REPO)
    assert records
    box.github.merge(REPO, 240)
    box.request()
    box.work()
    assert box.read().status_code == 404
    assert list((Path(box.cfg.state_dir) / "exams").rglob("threads.json")) == []
    assert box.records(REPO) == records


SKILLS = [Path(__file__).resolve().parent.parent / ".my-skills" / "pr-exam" / "pr_exam.py",
          Path.home() / ".my-skills" / "pr-exam" / "pr_exam.py"]


class RealChat(Recording):
    """Recording's fake generation and grading, with the console's own `PrExam.context` on the installed
    assist-capable skill: only the model is faked."""

    def context(self, exam, diff, body, turn):
        script = next((s for s in SKILLS if s.exists() and 'add_parser("context")' in s.read_text()), None)
        assert script, "no installed pr-exam skill has a `context` command"
        return PrExam(script).context(exam, diff, body, turn)


class Real(Turns):
    PR_EXAM = RealChat


def test_the_real_context_contract_builds_a_tip_prompt_then_a_question_prompt_with_its_history(tmp_path, monkeypatch):
    box = Real(tmp_path, monkeypatch)
    body = opened(box)

    async def go():
        async with box.aclient() as c:
            r = await box.ask(c, body, "tip", "q5", "the token only grows")
            assert r.status_code == 202, r.text
            first = await box.settle(c, "q5")
            r = await box.ask(c, body, "question", "q5", "is that why it expires?")
            assert r.status_code == 202, r.text
            return first, await box.settle(c, "q5")

    first, thread = asyncio.run(go())
    assert [t["status"] for t in first] == ["done"] and [t["status"] for t in thread] == ["done", "done"]
    tip = box.stdin(0)
    assert "Tip number: 1" in tip and "Draft: the token only grows" in tip and "Diff:" in tip
    chat = box.stdin(1)
    assert "Message: is that why it expires?" in chat and "Diff:" in chat
    assert "the token only grows" in chat and REPLY in chat
