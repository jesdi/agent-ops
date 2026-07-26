"""FastAPI app factory: route wiring only. All I/O is behind `sources`."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from dispatcher import queue_ops
from dispatcher.config import Config, policy_for
from dispatcher.models import resolve
from web import read_model
from web.auth import Operator, TailscaleAuthMiddleware, current_operator


class BoostReq(BaseModel):
    issue: int
    amount: int


class NextReq(BaseModel):
    issue: int
    force: bool = False


class ReadyReq(BaseModel):
    issue: int


def create_app(cfg: Config, sources) -> FastAPI:
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

    return app
