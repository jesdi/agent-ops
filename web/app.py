"""FastAPI app factory: route wiring only. All I/O is behind `sources`."""
from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, StreamingResponse
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from dispatcher import queue_ops
from dispatcher.config import Config, policy_for
from dispatcher.models import resolve
from web import read_model
from web.auth import Operator, TailscaleAuthMiddleware, current_operator
from web.terminal import run_terminal

DEFAULT_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class SPAStaticFiles(StaticFiles):
    """404s on non-file paths fall back to index.html (client routing)."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise

SSE_KEYS = ("board", "queue", "budget", "failures", "history")
HEARTBEAT_SECONDS = 15.0


class BoostReq(BaseModel):
    issue: int
    amount: int


class ReplyReq(BaseModel):
    text: str = Field(min_length=1)


class ResumeReq(BaseModel):
    text: str = ""


class NextReq(BaseModel):
    issue: int
    force: bool = False


class ReadyReq(BaseModel):
    issue: int


def create_app(cfg: Config, sources, sse_interval: float = 1.0,
               heartbeat_seconds: float = HEARTBEAT_SECONDS,
               frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="agent-ops web console")
    app.add_middleware(TailscaleAuthMiddleware)

    @app.get("/api/health")
    def health(op: Operator = Depends(current_operator)):
        return {"ok": True, "operator": op.login}

    targets_by_name = {t.name: t for t in cfg.targets}

    def _model_for(t):
        target = targets_by_name.get(t.target)
        policy = policy_for(cfg, target) if target else cfg.models
        return resolve(policy, t.stage.value, t.effort, list(t.labels))

    @app.get("/api/board", response_model=read_model.BoardView)
    def board(op: Operator = Depends(current_operator)):
        tasks = sources.tasks()
        return read_model.build_board(
            tasks, capacity=cfg.capacity,
            models={t.issue: _model_for(t) for t in tasks},
            attached={t.issue for t in tasks
                      if sources.has_attached(t.issue)})

    @app.get("/api/task/{issue}", response_model=read_model.TaskDetail)
    def task_detail(issue: int,
                    op: Operator = Depends(current_operator)):
        match = [t for t in sources.tasks() if t.issue == issue]
        if not match:
            raise HTTPException(404, f"no task {issue}")
        t = match[0]
        return read_model.task_detail(
            t, model=_model_for(t),
            attached=sources.has_attached(issue),
            pane_tail=sources.pane_tail(issue),
            session_alive=sources.session_alive(issue))

    @app.get("/api/budget", response_model=read_model.BudgetView)
    def budget_route(op: Operator = Depends(current_operator)):
        return read_model.budget_view(
            sources.usage(), cfg.budget_threshold, cfg.racing_minutes,
            cfg.racing_threshold)

    @app.get("/api/failures", response_model=read_model.FailuresView)
    def failures(op: Operator = Depends(current_operator)):
        quarantined = [
            read_model.QuarantineEntry(
                **q, blocker_open=sources.issue_open(
                    q["blocker_repo"], q["blocker_issue"]))
            for q in sources.quarantine_entries()]
        fingerprints = [read_model.FingerprintEntry(**f)
                        for f in sources.fingerprint_entries()]
        return read_model.FailuresView(quarantined=quarantined,
                                       fingerprints=fingerprints)

    @app.get("/api/history", response_model=read_model.HistoryView)
    def history(limit: int = 200,
                op: Operator = Depends(current_operator)):
        return read_model.HistoryView(
            events=[read_model.EventEntry(**e)
                    for e in sources.events_tail(limit)])

    def _locate_row(issue: int):
        hits = [(target, row)
                for target in cfg.targets
                for row in sources.rank_rows(target)[0]
                if row["number"] == issue]
        if not hits:
            raise HTTPException(404, f"issue {issue} not on any queue")
        if len(hits) > 1:
            raise HTTPException(
                409, f"issue {issue} is ambiguous across targets")
        return hits[0]

    def _apply(event: str, issue: int, plan, op: Operator, detail: str,
               target):
        if not plan.ok:
            raise HTTPException(422, plan.reason)
        sources.apply_queue_plan(target, issue, plan)
        sources.append_event(event, target=target.name, issue=issue,
                             actor=op.login, detail=detail)
        return {"ok": True, "reason": plan.reason}

    @app.post("/api/queue/boost")
    def queue_boost(req: BoostReq,
                    op: Operator = Depends(current_operator)):
        target, row = _locate_row(req.issue)
        return _apply("queue-boost", req.issue,
                      queue_ops.plan_boost(row, req.amount), op,
                      f"amount={req.amount}", target)

    @app.post("/api/queue/next")
    def queue_next(req: NextReq,
                   op: Operator = Depends(current_operator)):
        target, row = _locate_row(req.issue)
        return _apply("queue-next", req.issue,
                      queue_ops.plan_next(row, req.force), op,
                      f"force={req.force}", target)

    @app.post("/api/queue/ready")
    def queue_ready(req: ReadyReq,
                    op: Operator = Depends(current_operator)):
        target, row = _locate_row(req.issue)
        return _apply("queue-ready", req.issue,
                      queue_ops.plan_ready(row), op, "", target)

    @app.get("/api/queue", response_model=read_model.QueueView)
    def queue_view(op: Operator = Depends(current_operator)):
        in_flight = {t.issue for t in sources.tasks()}
        return read_model.QueueView(targets=[
            read_model.target_queue(target.name, *sources.rank_rows(target),
                                    in_flight=in_flight)
            for target in cfg.targets])

    def _require_task(issue: int) -> None:
        if not any(t.issue == issue for t in sources.tasks()):
            raise HTTPException(404, f"no task {issue}")

    def _accepted(action: str, issue: int, payload: dict, op: Operator):
        name = sources.submit_intent(action, issue, payload, op.login)
        return JSONResponse({"status": "pending", "intent": name},
                            status_code=202)

    @app.post("/api/task/{issue}/reply", status_code=202)
    def intent_reply(issue: int, req: ReplyReq,
                     op: Operator = Depends(current_operator)):
        _require_task(issue)
        return _accepted("reply", issue, {"text": req.text}, op)

    @app.post("/api/task/{issue}/park", status_code=202)
    def intent_park(issue: int,
                    op: Operator = Depends(current_operator)):
        _require_task(issue)
        return _accepted("park", issue, {}, op)

    @app.post("/api/task/{issue}/kill", status_code=202)
    def intent_kill(issue: int,
                    op: Operator = Depends(current_operator)):
        _require_task(issue)
        return _accepted("kill", issue, {}, op)

    @app.post("/api/task/{issue}/retry", status_code=202)
    def intent_retry(issue: int,
                     op: Operator = Depends(current_operator)):
        if not any(q.get("task_issue") == issue
                   for q in sources.quarantine_entries()):
            raise HTTPException(404, f"issue {issue} is not quarantined")
        return _accepted("retry", issue, {}, op)

    @app.post("/api/task/{issue}/resume", status_code=202)
    def intent_resume(issue: int, req: ResumeReq,
                      op: Operator = Depends(current_operator)):
        _require_task(issue)
        payload = {"text": req.text} if req.text else {}
        return _accepted("resume", issue, payload, op)

    @app.get("/api/pending-intents")
    def pending_intents(op: Operator = Depends(current_operator)):
        return {"intents": sources.pending_intents()}

    @app.get("/api/events")
    async def events(request: Request,
                     op: Operator = Depends(current_operator)):
        async def stream():
            last = _json.loads(sources.state_fingerprint())
            quiet = 0.0
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(sse_interval)
                cur = _json.loads(sources.state_fingerprint())
                changed = [k for k in SSE_KEYS if cur.get(k) != last.get(k)]
                if changed:
                    last = cur
                    quiet = 0.0
                    yield "data: " + _json.dumps({"changed": changed}) + "\n\n"
                    continue
                quiet += sse_interval
                if quiet >= heartbeat_seconds:
                    quiet = 0.0
                    yield ": heartbeat\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"},
        )

    @app.websocket("/api/task/{issue}/terminal")
    async def terminal(ws: WebSocket, issue: int):
        # auth already enforced by TailscaleAuthMiddleware (4401 close)
        await run_terminal(ws, issue, sources)

    dist = frontend_dist if frontend_dist is not None else DEFAULT_DIST
    if dist.is_dir():
        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="spa")
    else:
        @app.get("/")
        def root(op: Operator = Depends(current_operator)):
            return {"service": "agent-ops-web", "ui": "not built"}

    return app
