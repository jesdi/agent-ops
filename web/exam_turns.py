"""Exam chat turns: a tip or a message starts a model run that the console owns as a
background task, so closing the page changes nothing. `POST .../turns` reserves the turn and
answers at once; `GET .../turns/{id}/stream` replays and follows its reply. The finished turn
is stored in the attempt's `threads.json` and read back through the exam view. The rules
(which attempt takes which turn, the prompt, the model) are the `Sources` seam's; this
router does the HTTP, the stream and the task."""
from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from dispatcher.config import Config
from dispatcher.exam_threads import Ask
from web.auth import Operator, current_operator
from web.exams import Owner, Pr
from web.sources import StartedTurn

POLL = 0.05    # seconds between looks at a live reply; ponytail: a condition variable if it ever shows
KEPT = 64      # finished replies kept for a stream that opens late


class _Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: int = Field(strict=True)
    head_sha: str
    message: str


class QuestionTurn(_Turn):
    """A tip on a question, or a message in its thread."""
    kind: Literal["tip", "question"]
    question: str


class GeneralTurn(_Turn):
    """A message in the attempt's general chat."""
    kind: Literal["general"]
    question: None = None


TurnBody = Annotated[QuestionTurn | GeneralTurn, Field(discriminator="kind")]


@dataclass
class Live:
    """A reply as it is written: its chunks, and `final` once it has ended."""
    repo: str
    pr: int
    chunks: list[str] = field(default_factory=list)
    final: dict | None = None


def _event(doc: dict) -> str:
    return "data: " + json.dumps(doc) + "\n\n"


async def _converse(live: Live, turn: StartedTurn) -> str:
    """Run the model command with the prompt on stdin, feeding every text chunk to `live`;
    "" when it exited 0 with a reply, else why it did not."""
    proc = await asyncio.create_subprocess_exec(*turn.argv, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, limit=1 << 22)
    errors = asyncio.create_task(proc.stderr.read())
    try:
        proc.stdin.write(turn.prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        async for line in proc.stdout:
            if chunk := _chunk(line, turn.text_of):
                live.chunks.append(chunk)
        stopped = _stopped(await proc.wait(), await errors)
        return stopped or ("" if "".join(live.chunks).strip() else "the model sent no reply")
    finally:
        errors.cancel()
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


def _chunk(line: bytes, text_of: Callable[[dict], str]) -> str:
    try:
        return text_of(json.loads(line))
    except (ValueError, AttributeError):   # not a JSON object: the CLI's own noise
        return ""


def _stopped(code: int, stderr: bytes) -> str:
    """"" for exit 0, else the exit code and the last line of stderr."""
    tail = stderr.decode(errors="replace").strip().splitlines()[-1:]
    return f"the model stopped: exit {code}" + (f": {tail[0]}" if tail else "") if code else ""


async def _reply(live: Live, turn: StartedTurn) -> str:
    """The turn's model run; "" or why it failed. Never raises."""
    try:
        async with asyncio.timeout(turn.timeout):
            return await _converse(live, turn)
    except TimeoutError:
        return f"the model ran past {turn.timeout // 60} minutes"
    except Exception as exc:   # a turn must end, whatever went wrong
        return f"the model could not run: {exc}"


def router(sources, cfg: Callable[[], Config], now: Callable[[], datetime]) -> APIRouter:
    routes = APIRouter()
    lives: dict[str, Live] = {}
    tasks: set[asyncio.Task] = set()

    async def run(live: Live, turn: StartedTurn, attempt: int) -> None:
        """Run the turn and store how it ended; its stream always ends, as the stored turn does."""
        final = {"status": "failed", "error": "the turn stopped"}
        try:
            error = await _reply(live, turn)
            error = await asyncio.to_thread(sources.end_turn, live.repo, live.pr, attempt, turn.id,
                                            "".join(live.chunks), error) or error
            final = {"status": "failed", "error": error} if error else {"status": "done"}
        except Exception as exc:   # the reply could not be stored: fail the turn, best effort
            final = {"status": "failed", "error": f"the reply could not be stored: {exc}"}
            with contextlib.suppress(Exception):
                await asyncio.to_thread(sources.end_turn, live.repo, live.pr, attempt, turn.id, "", final["error"])
        finally:
            live.final = final

    @routes.post("/api/exam/{owner}/{name}/{pr}/turns", status_code=202)
    async def start(body: TurnBody, owner: Owner, name: Owner, pr: Pr, op: Operator = Depends(current_operator)):
        repo = f"{owner}/{name}"
        turn = await asyncio.to_thread(sources.start_turn, cfg(), now(), repo, pr, Ask(**body.model_dump()))
        live = lives[turn.id] = Live(repo, pr)
        while len(lives) > KEPT:
            del lives[next(iter(lives))]
        task = asyncio.create_task(run(live, turn, body.attempt))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"id": turn.id}

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
