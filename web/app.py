"""FastAPI app factory: route wiring only. All I/O is behind `sources`."""
from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timezone
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
from web.auth import (HEADER, Operator, TailscaleAuthMiddleware,
                      current_operator)
from web.terminal import AttachRegistry, run_terminal

DEFAULT_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class SPAStaticFiles(StaticFiles):
    """404s on non-file paths fall back to index.html (client routing)."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not path.lstrip("/").startswith(
                    "api/"):
                return await super().get_response("index.html", scope)
            # An unmatched /api/* path is a missing route, not a client-side
            # one: answering it with the SPA shell surfaces in the frontend
            # as a JSON parse error instead of a 404.
            raise

SSE_KEYS = ("board", "queue", "budget", "failures", "history")
HEARTBEAT_SECONDS = 15.0
EVENTS_SCAN_LIMIT = 5000


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

    def _known_target(target: str, tasks: list) -> bool:
        """A target is servable if it is still in the live config OR any
        task in state currently carries it — the latter keeps a Done card
        viewable after its target is removed from targets.yaml."""
        return target in targets_by_name or any(
            t.target == target for t in tasks)

    def _find_task(target: str, issue: int):
        """The task at (target, issue), or a 404 — unknown target and
        known-target-but-no-such-task are both reported as 404, distinguished
        only by detail text."""
        tasks = sources.tasks()
        match = [t for t in tasks if t.target == target and t.issue == issue]
        if match:
            return match[0]
        if not _known_target(target, tasks):
            raise HTTPException(404, f"unknown target {target!r}")
        raise HTTPException(404, f"no task {target}/{issue}")

    @app.get("/api/board", response_model=read_model.BoardView)
    def board(op: Operator = Depends(current_operator)):
        tasks = sources.tasks()
        claims_paused, triage_running = sources.triage_state()
        queues, stale_any = [], False
        for target in cfg.targets:
            rows, _as_of, stale = sources.rank_rows(target)
            stale_any = stale_any or stale
            queues.append((target.name, rows))
        # undelivered_counts() is still issue-keyed — dispatcher/messages.py
        # is a deferred rekey, tracked as a known cross-target gap (a task's
        # message queue can still cross with a same-numbered issue on
        # another target). Paired here with each task's OWN target rather
        # than left bare, so at least this dict's shape matches every other
        # per-card lookup on the board.
        mail = sources.undelivered_counts()
        return read_model.build_board(
            tasks, capacity=cfg.capacity,
            models={(t.target, t.issue): _model_for(t) for t in tasks},
            attached={(t.target, t.issue) for t in tasks
                      if sources.has_attached(t.target, t.issue)},
            events=sources.events_tail(EVENTS_SCAN_LIMIT),
            heartbeat=sources.pass_heartbeat(),
            now=datetime.now(timezone.utc),
            budget=read_model.budget_view(
                sources.usage(), cfg.budget_threshold, cfg.racing_minutes,
                cfg.racing_threshold),
            queues=queues, queue_stale=stale_any,
            # One tmux probe for both signals (cf. dispatcher run_pass).
            claims_paused=claims_paused, triage_running=triage_running,
            undelivered={(t.target, t.issue): mail.get(t.issue, 0)
                        for t in tasks},
            wake_blocked=sources.wake_blocked_issues())

    @app.get("/api/task/{target}/{issue}",
             response_model=read_model.TaskDetail)
    def task_detail(target: str, issue: int,
                    op: Operator = Depends(current_operator)):
        t = _find_task(target, issue)
        # Legacy intent files predate the target field (Task 3); they carry
        # target="" and still belong to whichever task's issue they name —
        # i.get("target") in ("", target) keeps them showing up here instead
        # of vanishing.
        pending = [i for i in sources.pending_intents()
                   if i.get("issue") == issue
                   and i.get("target", "") in ("", target)
                   and i.get("action") in ("reply", "resume")]
        return read_model.task_detail(
            t, model=_model_for(t),
            attached=sources.has_attached(target, issue),
            pane_tail=sources.pane_tail(target, issue),
            session_alive=sources.session_alive(target, issue),
            events=sources.events_tail(EVENTS_SCAN_LIMIT),
            now=datetime.now(timezone.utc),
            messages=sources.messages(issue),
            pending_sends=pending,
            wake_blocked=(target, issue) in sources.wake_blocked_issues())

    @app.get("/api/task/{target}/{issue}/description",
             response_model=read_model.IssueDescription)
    def task_description(target: str, issue: int,
                         op: Operator = Depends(current_operator)):
        # `target` is already given, so — unlike the old issue-only route —
        # there is no cross-target AMBIGUITY left to resolve or 409 on: the
        # repo comes straight from this target's config, never scanned for
        # across cfg.targets. But the issue must still be real on this
        # target: either a claimed task (state) or a ghost still on this
        # target's own queue (rank_rows) — anything else is a true 404, the
        # same "never a blank, always an explicit state" contract the
        # sibling routes hold to.
        tgt = targets_by_name.get(target)
        if tgt is None:
            raise HTTPException(404, f"unknown target {target!r}")
        is_task = any(t.target == target and t.issue == issue
                      for t in sources.tasks())
        is_ghost = any(r["number"] == issue
                       for r in sources.rank_rows(tgt)[0])
        if not (is_task or is_ghost):
            raise HTTPException(404, f"issue {target}/{issue} is neither a "
                                     "task nor on this target's queue")
        return read_model.IssueDescription(
            **sources.issue_description(tgt.repo, issue))

    @app.get("/api/task/{target}/{issue}/spec",
             response_model=read_model.SpecView)
    def task_spec(target: str, issue: int,
                  op: Operator = Depends(current_operator)):
        t = _find_task(target, issue)
        wt = Path(t.worktree).resolve()
        p = Path(t.artifact).resolve() if t.artifact else None
        # Anything short of a readable file inside the worktree is "no spec":
        # the artifact path is dispatcher-written state, not user input, but
        # the worktree check keeps a corrupted state file from reading /etc.
        if p is None or not p.is_relative_to(wt):
            raise HTTPException(
                404, f"no spec recorded for task {target}/{issue}")
        try:
            markdown = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise HTTPException(
                404, f"spec file missing for task {target}/{issue}")
        return read_model.SpecView(path=str(p.relative_to(wt)),
                                   markdown=markdown)

    HISTORY_MAX_LINES = 10000

    @app.get("/api/task/{target}/{issue}/history",
             response_model=read_model.PaneHistory)
    def task_history(target: str, issue: int, lines: int = 2000,
                     op: Operator = Depends(current_operator)):
        _find_task(target, issue)
        # Unbounded `lines` would let one request pull an entire pane history
        # into memory and over the wire; clamp before hitting tmux.
        clamped = max(1, min(lines, HISTORY_MAX_LINES))
        return read_model.PaneHistory(
            text=sources.pane_history(target, issue, clamped))

    @app.get("/api/budget", response_model=read_model.BudgetView)
    def budget_route(op: Operator = Depends(current_operator)):
        return read_model.budget_view(
            sources.usage(), cfg.budget_threshold, cfg.racing_minutes,
            cfg.racing_threshold)

    @app.get("/api/failures", response_model=read_model.FailuresView)
    def failures(op: Operator = Depends(current_operator)):
        quarantined = [
            read_model.QuarantineEntry(
                # A record with no blocker has nothing to look up: asking gh
                # about issue 0 costs a 120 s subprocess to learn nothing.
                **q, blocker_open=(sources.issue_open(q["blocker_repo"],
                                                      q["blocker_issue"])
                                   if q["blocker_issue"] else None))
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

    def _stale(as_of: str) -> HTTPException:
        return HTTPException(
            409, f"queue data is stale (as of {as_of}); "
                 "retry when GitHub is reachable")

    def _locate_row(issue: int):
        """Find the row a queue write targets, refusing stale-backed writes.

        When GitHub is unreachable the rank cache keeps the VIEW useful, but
        a write would evaluate a row whose real state may have moved on and
        then push a synchronous GitHub change on that basis.  A cold cache
        additionally yields no rows at all, which must report staleness
        rather than masquerade as an unknown issue.
        """
        hits = []
        stale_as_of = None
        for target in cfg.targets:
            rows, as_of, stale = sources.rank_rows(target)
            if stale and stale_as_of is None:
                stale_as_of = as_of
            hits += [(target, row, stale, as_of)
                     for row in rows if row["number"] == issue]
        if not hits:
            if stale_as_of is not None:
                raise _stale(stale_as_of)
            raise HTTPException(404, f"issue {issue} not on any queue")
        if len(hits) > 1:
            raise HTTPException(
                409, f"issue {issue} is ambiguous across targets")
        target, row, stale, as_of = hits[0]
        if stale:
            raise _stale(as_of)
        return target, row

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

    def _require_task(target: str, issue: int) -> None:
        if not any(t.target == target and t.issue == issue
                   for t in sources.tasks()):
            raise HTTPException(404, f"no task {target}/{issue}")

    def _accepted(action: str, target: str, issue: int, payload: dict,
                 op: Operator):
        name = sources.submit_intent(action, target, issue, payload,
                                     op.login)
        return JSONResponse({"status": "pending", "intent": name},
                            status_code=202)

    @app.post("/api/task/{target}/{issue}/reply", status_code=202)
    def intent_reply(target: str, issue: int, req: ReplyReq,
                     op: Operator = Depends(current_operator)):
        # NO _require_task: the contract is "never drop a message", and that
        # includes an unclaimed backlog issue (a pre-briefing delivered at
        # claim time) and a done/failed tombstone (delivered if it restarts).
        # The queue is keyed by issue and survives every task lifecycle, so
        # there is nothing here to 404 against.
        return _accepted("reply", target, issue, {"text": req.text}, op)

    @app.post("/api/task/{target}/{issue}/park", status_code=202)
    def intent_park(target: str, issue: int,
                    op: Operator = Depends(current_operator)):
        _require_task(target, issue)
        return _accepted("park", target, issue, {}, op)

    @app.post("/api/task/{target}/{issue}/kill", status_code=202)
    def intent_kill(target: str, issue: int,
                    op: Operator = Depends(current_operator)):
        _require_task(target, issue)
        return _accepted("kill", target, issue, {}, op)

    @app.post("/api/task/{target}/{issue}/cancel", status_code=202)
    def intent_cancel(target: str, issue: int,
                      op: Operator = Depends(current_operator)):
        # NO _require_task: a backlog card with no task file cancels too —
        # the target in the path lets the dispatcher retire the board card.
        return _accepted("cancel", target, issue, {}, op)

    @app.post("/api/task/{target}/{issue}/retry", status_code=202)
    def intent_retry(target: str, issue: int,
                     op: Operator = Depends(current_operator)):
        if not any(q.get("target") == target and q.get("task_issue") == issue
                   for q in sources.quarantine_entries()):
            raise HTTPException(
                404, f"issue {target}/{issue} is not quarantined")
        return _accepted("retry", target, issue, {}, op)

    @app.post("/api/task/{target}/{issue}/resume", status_code=202)
    def intent_resume(target: str, issue: int, req: ResumeReq,
                      op: Operator = Depends(current_operator)):
        _require_task(target, issue)
        payload = {"text": req.text} if req.text else {}
        return _accepted("resume", target, issue, payload, op)

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

    viewers = AttachRegistry(sources)

    @app.websocket("/api/task/{target}/{issue}/terminal")
    async def terminal(ws: WebSocket, target: str, issue: int):
        # auth and same-origin already enforced by TailscaleAuthMiddleware
        # (4401 / 4403 close); the header is present by that point.
        await run_terminal(ws, target, issue, sources, viewers,
                           actor=ws.headers.get(HEADER, ""))

    dist = frontend_dist if frontend_dist is not None else DEFAULT_DIST
    if dist.is_dir():
        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="spa")
    else:
        # include_in_schema=False: this stub stands in for the SPA shell, not
        # for an API endpoint. Keeping it out of the schema makes `pnpm gen:api`
        # output identical whether or not frontend/dist has been built.
        @app.get("/", include_in_schema=False)
        def root(op: Operator = Depends(current_operator)):
            return {"service": "agent-ops-web", "ui": "not built"}

    return app
