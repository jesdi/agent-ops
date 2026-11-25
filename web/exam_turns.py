"""Exam chat turns: a tip or a message starts a model run that the console owns as a
background task, so closing the page changes nothing. `POST .../turns` reserves the turn and
answers at once; `GET .../turns/{id}/stream` replays and follows its reply. The finished turn
is stored in the attempt's `threads.json` and read back through the exam view."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from dispatcher import containers, exams
from dispatcher.config import Config
from dispatcher.exam_runner import PrExam
from dispatcher.runtimes import runtime_for
from web.auth import Operator, current_operator
from web.exams import Owner, Pr

POLL = 0.05    # seconds between looks at a live reply; ponytail: a condition variable if it ever shows
KEPT = 64      # finished replies kept for a stream that opens late


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: int = Field(strict=True)
    head_sha: str
    kind: Literal["tip", "question", "general"]
    question: str | None = None
    message: str


@dataclass
class Live:
    """A reply as it is written: its chunks, and `final` once it has ended."""
    repo: str
    pr: int
    chunks: list[str] = field(default_factory=list)
    final: dict | None = None


def _event(doc: dict) -> str:
    return "data: " + json.dumps(doc) + "\n\n"


async def _converse(live: Live, argv: list[str], prompt: str, text_of: Callable[[dict], str]) -> str:
    """Run the model command with `prompt` on stdin, feeding every text delta to `live`;
    "" when it exited 0, else why it did not."""
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, limit=1 << 22)
    errors = asyncio.create_task(proc.stderr.read())
    try:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        async for line in proc.stdout:
            if delta := _delta(line, text_of):
                live.chunks.append(delta)
        return _stopped(await proc.wait(), await errors)
    finally:
        errors.cancel()
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


def _delta(line: bytes, text_of: Callable[[dict], str]) -> str:
    try:
        return text_of(json.loads(line))
    except (ValueError, AttributeError):   # not a JSON object: the CLI's own noise
        return ""


def _stopped(code: int, stderr: bytes) -> str:
    """"" for exit 0, else the exit code and the last line of stderr."""
    tail = stderr.decode(errors="replace").strip().splitlines()[-1:]
    return f"the model stopped: exit {code}" + (f": {tail[0]}" if tail else "") if code else ""


def _refuse(refusal: str) -> None:
    """Turn `turn_check` or `turn_start`'s refusal into its HTTP error."""
    if refusal:
        raise HTTPException(*{"conflict": (409, "this attempt cannot take that turn"),
                              "unknown": (422, "the exam has no such question"),
                              "busy": (409, "a turn is already running there")}[refusal])


def router(cfg: Callable[[], Config], usages: Callable[[Config], dict], now: Callable,
           pr_exam: PrExam | None = None) -> APIRouter:
    pr_exam = pr_exam or PrExam()
    routes = APIRouter()
    lives: dict[str, Live] = {}
    tasks: set[asyncio.Task] = set()

    async def run(live: Live, turn_id: str, attempt: int, prompt: str, model: str, effort: str,
                  live_cfg: Config) -> None:
        try:
            error = ""
            try:
                argv = containers.turn_cmd(f"turn-{turn_id}", str(live_cfg.state_dir), live_cfg.session_memory,
                                           live_cfg.session_cpus, model, effort)
                async with asyncio.timeout(exams.TIMEOUT):
                    error = await _converse(live, argv, prompt, runtime_for(model).stream_text)
            except TimeoutError:
                error = f"the model ran past {exams.TIMEOUT // 60} minutes"
            except Exception as exc:   # a turn must end, whatever went wrong
                error = f"the model could not run: {exc}"
            dropped = await asyncio.to_thread(exams.turn_end, live_cfg.state_dir, live.repo, live.pr, attempt,
                                              turn_id, "".join(live.chunks), error)
        finally:
            exams.turn_release(live_cfg.state_dir, turn_id)
        error = dropped or error
        live.final = {"status": "failed", "error": error} if error else {"status": "done"}

    @routes.post("/api/exam/{owner}/{name}/{pr}/turns", status_code=202)
    async def start(body: TurnBody, owner: Owner, name: Owner, pr: Pr, op: Operator = Depends(current_operator)):
        repo, live_cfg = f"{owner}/{name}", cfg()
        if (body.kind == "general") != (body.question is None):
            raise HTTPException(422, "a general turn takes no question, any other turn needs one")
        where = (live_cfg.state_dir, repo, pr, body.attempt, body.head_sha, body.kind, body.question)
        _refuse(await asyncio.to_thread(exams.turn_check, *where))
        entry = await asyncio.to_thread(lambda: exams.entry(live_cfg, usages(live_cfg), now()))
        if entry is None:
            raise HTTPException(429, "unavailable: usage limit")
        turn = {"kind": body.kind, "question": body.question, "message": body.message,
                "thread": await asyncio.to_thread(exams.thread, live_cfg.state_dir, repo, pr, body.attempt, body.question)}
        try:
            prompt = await asyncio.to_thread(pr_exam.context,
                                             exams.attempt_dir(live_cfg.state_dir, repo, pr, body.attempt), turn)
        except ValueError:
            raise HTTPException(422, "PR too large for chat")
        refusal, turn_id = await asyncio.to_thread(exams.turn_start, *where, body.message)
        _refuse(refusal)
        live = lives[turn_id] = Live(repo, pr)
        while len(lives) > KEPT:
            del lives[next(iter(lives))]
        task = asyncio.create_task(run(live, turn_id, body.attempt, prompt, entry.model_id, entry.effort, live_cfg))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"id": turn_id}

    @routes.get("/api/exam/{owner}/{name}/{pr}/turns/{turn_id}/stream")
    async def stream(request: Request, owner: Owner, name: Owner, pr: Pr, turn_id: str,
                     op: Operator = Depends(current_operator)):
        live = lives.get(turn_id)
        if live is None or (live.repo, live.pr) != (f"{owner}/{name}", pr):
            raise HTTPException(404, "turn not found")

        async def events():
            sent = 0
            while True:
                final = live.final   # read first: a chunk written after it is sent by the next look
                fresh = live.chunks[sent:]
                sent += len(fresh)
                if fresh:
                    yield _event({"delta": "".join(fresh)})
                if final:
                    yield _event(final)
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(POLL)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return routes
