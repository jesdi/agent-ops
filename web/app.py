"""FastAPI app factory: route wiring only. All I/O is behind `sources`, with
two exceptions: the priority mode and the model settings are read and written
with `dispatcher.priority` and `dispatcher.model_settings` on the state dir,
the one reader the dispatcher and triage also use."""
from __future__ import annotations

import asyncio
import json as _json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import (BaseModel, Field, TypeAdapter, ValidationError,
                      field_validator)
from starlette.responses import JSONResponse, StreamingResponse
from starlette.staticfiles import StaticFiles
from dispatcher import execution_overrides, model_settings, priority, queue_ops
from dispatcher.config import (Config, apply_stored_models, normalize_repo,
                               policy_for, project_target,
                               referenced_providers, routed_providers,
                               stranded, with_models)
from dispatcher.convergence import pass_lock
from dispatcher.models import (ModelPolicy, candidates, override_allowed,
                               override_refusal, parse_entry,
                               stage_pick, track_from_labels)
from dispatcher.usage import admits
from dispatcher import answers as answers_file, intents
from dispatcher.state import (TERMINAL_STAGES, PARK_WAKE, TaskState,
                              launch_entries, launch_track, next_stage,
                              resumable_crash)
from web import model_settings_view as msv
from web import read_model
from web.artifacts import router as artifacts_router
from web.auth import (Operator, TailscaleAuthMiddleware,
                      current_operator)
from web.task_ref import REF, TaskRef, ambiguous, task_routes

DEFAULT_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class SPAStaticFiles(StaticFiles):
    """404s on non-file paths fall back to index.html (client routing).

    The shell is served no-cache: StaticFiles sets only ETag/Last-Modified,
    and browsers (phones especially) then cache index.html heuristically for
    weeks, pinning a bundle whose API paths the backend no longer has.
    Vite's /assets/* are content-hashed, so those cache forever."""

    async def get_response(self, path, scope):
        try:
            resp = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not path.lstrip("/").startswith(
                    "api/"):
                resp = await super().get_response("index.html", scope)
                path = "index.html"
            else:
                # An unmatched /api/* path is a missing route, not a
                # client-side one: answering it with the SPA shell surfaces
                # in the frontend as a JSON parse error instead of a 404.
                raise
        if path.lstrip("/").startswith("assets/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp

SSE_KEYS = ("board", "queue", "usage", "failures", "history")
HEARTBEAT_SECONDS = 15.0
EVENTS_SCAN_LIMIT = 5000
# How long a model-settings save waits for a dispatcher pass to finish.
PASS_LOCK_WAIT_SECONDS = 30.0


class BoostReq(BaseModel):
    issue: int
    amount: int
    repo: str = ""      # "owner/name"; "" = bare, valid while one row has the number


class ReplyReq(BaseModel):
    text: str = Field(min_length=1)


ANSWERS_MAX_BYTES = 64 * 1024
ANSWER_KEY = re.compile(rf"^({answers_file.QUESTION_ID}(\.note)?|track)$")
_ANSWERS = TypeAdapter(read_model.Answers)


class AnswersReq(BaseModel):
    answers: read_model.Answers
    submit: Literal[answers_file.SUBMITS] | None = None
    revision: str = Field(min_length=1)

    @field_validator("answers")
    @classmethod
    def _keys(cls, v):
        bad = [k for k in v if not ANSWER_KEY.match(k)]
        if bad:
            raise ValueError(f"bad answer key {bad[0]!r}")
        return v


async def _capped_body(request: Request) -> None:
    if len(await request.body()) > ANSWERS_MAX_BYTES:
        raise HTTPException(422, "answers body over 64 KiB")


class ResumeReq(BaseModel):
    text: str = ""
    model: str = ""
    bypass_usage: bool = False


class RunReq(BaseModel):
    model: str = ""
    bypass_usage: bool = False


class PriorityReq(BaseModel):
    mode: str


class NextReq(BaseModel):
    issue: int
    repo: str = ""
    force: bool = False


class ReadyReq(BaseModel):
    issue: int
    repo: str = ""


def launch_pinned_track(t: TaskState, policy: ModelPolicy, *,
                        overridden: bool = False) -> str:
    """The pinned track t's next launch comes from, else "": the launch's
    track (state.launch_track: the task track) when it is pinned, unless no launch comes from the
    list — a terminal task or a pending one-shot override (`overridden`). A
    pick keeps the pin."""
    if t.stage in TERMINAL_STAGES or overridden:
        return ""
    track = launch_track(t, policy)
    return track if track in policy.pinned else ""


def create_app(boot: Config, sources, sse_interval: float = 1.0,
               heartbeat_seconds: float = HEARTBEAT_SECONDS,
               frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="agent-ops web console")
    app.add_middleware(TailscaleAuthMiddleware)

    @app.get("/api/health")
    def health(op: Operator = Depends(current_operator)):
        return {"ok": True, "operator": op.login}

    projects_by_name = {p.key: p for p in boot.projects}
    targets_by_name = {t.name: t for t in [*boot.targets,
                                          *map(project_target, boot.projects)]}

    def _target(name: str, repo: str = ""):
        project = projects_by_name.get(name)
        return project_target(project, repo) if project else targets_by_name.get(name)

    def _rank_rows(target):
        return sources.rank_rows(projects_by_name.get(target.name, target))

    def _cfg() -> Config:
        """The config in force now: `boot` with the stored model settings
        read again, as each dispatcher pass does. No copy to keep in step."""
        # ponytail: one small file read per call; cache by revision if it shows in profiles.
        return apply_stored_models(boot)
    def _usages(live: Config):
        """Usage of every provider the config in force can launch on."""
        return sources.usage(referenced_providers(live))

    def _mode(live: Config) -> str:
        return priority.load(live.state_dir, routed_providers(live))

    def _order(mode, usages=None, now=None):
        """The dispatcher's own ordering under `mode`. A route reads the mode
        and builds this once, so one response never mixes two modes. Without
        usage (the snapshot must not wait for it) a fixed mode still applies
        and auto is the written order."""
        return priority.order(mode, usages or {},
                              now or datetime.now(timezone.utc), boot.pace)

    def _policy(live: Config, target_name, repo: str = ""):
        target = _target(target_name, repo)
        return policy_for(live, target)

    def _require_same_provider(t, model):
        """422 unless `model` may override t's next launch: once the stage
        has a pick, only a model of the pick's provider may."""
        refusal = override_refusal(t.picks, next_stage(t), model) if model else ""
        if refusal:
            raise HTTPException(422, refusal)

    def _one_shot(t):
        """The pending one-shot override of t's next launch, or None: the
        dispatcher's own rule (execution_overrides.pending)."""
        return execution_overrides.pending(
            sources.execution_override(t.target, t.issue, t.repo),
            t.resume_model_override if t.park == PARK_WAKE else "",
            t.resume_bypass_usage)

    def _choices(live: Config, t, order):
        """The ordered entries the dispatcher would walk for t's next launch."""
        return launch_entries(t, _policy(live, t.target, t.repo), order)

    def _model_for(live: Config, t, order, usages=None, now=None):
        override = _one_shot(t)
        if override and override.model:
            return override.model
        pick = stage_pick(t.picks, next_stage(t))
        if pick:
            return parse_entry(pick, "pick").model_id
        choices = _choices(live, t, order)
        if not choices:
            return ""
        if usages is not None:
            admitted = next((e for e in choices
                             if admits(usages, e.model_id, now, boot.pace).admitted), None)
            if admitted is not None:
                return admitted.model_id
        return choices[0].model_id

    def _task_admission(live: Config, t, order, usages, now):
        override = _one_shot(t)
        if t.stage in TERMINAL_STAGES or (override and override.bypass_usage):
            return None
        stage = next_stage(t)
        pinned = _pinned_track(live, t)
        # A pinned wait may be overridden with any model of the policy.
        offered = (_policy(live, t.target, t.repo).model_ids() if pinned
                   else [e.model_id for e in _choices(live, t, order)])
        return _admission_for_model(
            _model_for(live, t, order, usages, now),
            [m for m in offered if override_allowed(t.picks, stage, m)],
            usages, now, pinned_track=pinned)

    def _pinned_track(live: Config, t):
        override = _one_shot(t)
        return launch_pinned_track(t, _policy(live, t.target, t.repo),
                                   overridden=bool(override and override.model))

    def _admission_for_model(model, choices, usages, now, pinned_track=""):
        if not model:
            return None
        requested = read_model.model_admission_view(
            usages, now=now, pace=boot.pace, model=model)
        if requested.admitted:
            return None
        alternatives = [read_model.model_admission_view(
            usages, now=now, pace=boot.pace, model=m)
            for m in dict.fromkeys(choices) if m != requested.model]
        return read_model.TaskAdmissionView(
            requested=requested, alternatives=alternatives,
            pinned_track=pinned_track)

    def _candidate_choices(live: Config, target, row, order):
        policy = policy_for(live, target)
        track = track_from_labels(list(row.get("labels") or []), policy)
        return [e.model_id for e in candidates(
            policy, track, "spec", order=order)]

    def _candidate_model(live: Config, target, row, order):
        return _candidate_choices(live, target, row, order)[0]

    def _candidate_admissions(live: Config, queues, order, usages, now):
        return {read_model.identity_key(target.name, row["number"], row.get("repo", "")): admission
                for target, rows in queues for row in rows
                if read_model._is_candidate(row)
                and sources.execution_override(target.name, row["number"],
                                                row.get("repo", "")) is None
                and (admission := _admission_for_model(
                    _candidate_model(live, target, row, order),
                    _candidate_choices(live, target, row, order),
                    usages, now))}

    def _known_target(target: str, tasks: list) -> bool:
        """A target is servable if it is still in the live config OR any
        task in state currently carries it — the latter keeps a Done card
        viewable after its target is removed from targets.yaml."""
        return target in targets_by_name or any(
            t.target == target for t in tasks)

    def _resolve(ref: TaskRef):
        """The one active task `ref` names, None when none does. A ref with
        no repository names every task of that number: 409 when several."""
        found = [t for t in sources.tasks()
                 if (t.target, t.issue) == (ref.target, ref.issue)
                 and ref.repo in ("", t.repo)]
        known = found if ref.repo else found + sources.archived_named(ref.target, ref.issue)
        repos = {t.repo for t in known}
        if len(repos) > 1:
            raise ambiguous(ref.target, ref.issue, sorted(repos))
        return found[0] if found else None

    def _find_task(ref: TaskRef):
        """The task `ref` names, or a 404 — unknown target and
        known-target-but-no-such-task are both reported as 404, distinguished
        only by detail text."""
        task = _resolve(ref) or sources.task(*ref)
        if task is not None:
            return task
        if not _known_target(ref.target, sources.tasks()):
            raise HTTPException(404, f"unknown target {ref.target!r}")
        raise HTTPException(404, f"no task {ref.target}/{ref.issue}")

    @app.get("/api/board/snapshot", response_model=read_model.BoardSnapshot)
    def board_snapshot(op: Operator = Depends(current_operator)):
        # Local state only: cards must not wait for GitHub, usage or sessions.
        live = _cfg()
        tasks = sources.tasks()
        mail = {**sources.undelivered_counts(), **sources.undelivered_qualified_counts()}
        order = _order(_mode(live))
        return read_model.build_board_snapshot(
            tasks, capacity=boot.capacity,
            models={read_model.mail_key(t): _model_for(live, t, order) for t in tasks},
            pinned_tracks={read_model.mail_key(t): _pinned_track(live, t) for t in tasks},
            events=sources.events_tail(EVENTS_SCAN_LIMIT), queues=[],
            undelivered={read_model.mail_key(t): mail.get(read_model.mail_key(t), 0)
                         for t in tasks},
            wake_blocked=sources.wake_blocked_issues())
    app.include_router(artifacts_router(sources, _find_task))

    def _usage_view(live: Config, usages, now, mode) -> read_model.UsageView:
        return read_model.usage_view(
            usages, now=now, pace=boot.pace,
            default_model=live.models.gate_entry().model_id,
            mode=mode, routed=routed_providers(live), pinned=live.models.pinned)

    @app.get("/api/board", response_model=read_model.BoardView)
    def board(op: Operator = Depends(current_operator)):
        live = _cfg()
        tasks = sources.tasks()
        now = datetime.now(timezone.utc)
        usages = _usages(live)
        mode = _mode(live)
        order = _order(mode, usages, now)
        usage = _usage_view(live, usages, now, mode)
        claims_paused, triage_running = sources.triage_state()
        queues, queue_targets, stale_any = [], [], False
        for target in boot.targets:
            rows, _as_of, stale = sources.rank_rows(target)
            stale_any = stale_any or stale
            queues.append((target.name, rows))
            queue_targets.append((target, rows))
        for project in boot.projects:  # ranked across all its repositories
            rows, _as_of, stale = sources.rank_rows(project)
            stale_any = stale_any or stale
            queues.append((project.key, rows))
            by_repo = {normalize_repo(r.repo): [] for r in project.repositories}
            for row in rows:  # an untagged row belongs to the first repository
                by_repo.get(normalize_repo(row.get("repo", "")),
                            next(iter(by_repo.values()))).append(row)
            queue_targets.extend((project_target(project, r.repo),
                                  by_repo[normalize_repo(r.repo)])
                                 for r in project.repositories)
        mail = {**sources.undelivered_counts(), **sources.undelivered_qualified_counts()}
        return read_model.build_board(
            tasks, capacity=boot.capacity,
            models={read_model.mail_key(t): _model_for(live, t, order, usages, now)
                    for t in tasks},
            pinned_tracks={read_model.mail_key(t): _pinned_track(live, t) for t in tasks},
            events=sources.events_tail(EVENTS_SCAN_LIMIT),
            heartbeat=sources.pass_heartbeat(),
            now=now,
            gate=usage.gate,
            queues=queues, queue_stale=stale_any,
            # One session-layer probe for both signals (cf. dispatcher run_pass).
            claims_paused=claims_paused, triage_running=triage_running,
            undelivered={read_model.mail_key(t): mail.get(read_model.mail_key(t), 0)
                        for t in tasks},
            wake_blocked=sources.wake_blocked_issues(),
            admissions={read_model.mail_key(t): admission
                        for t in tasks
                        if (admission := _task_admission(live, t, order, usages, now))},
            candidate_admissions=_candidate_admissions(live, queue_targets, order,
                                                       usages, now),
            max_active={**{t.name: t.max_active for t in boot.targets},
                        **{p.key: project_target(p).max_active for p in boot.projects}},
            last_claimed=sources.last_claims(),
            max_open=boot.max_open, projects=boot.projects)

    @task_routes(app, "get", response_model=read_model.TaskDetail)
    def task_detail(ref: TaskRef = REF,
                    op: Operator = Depends(current_operator)):
        t = _find_task(ref)
        target, issue = t.target, t.issue
        live = _cfg()
        now = datetime.now(timezone.utc)
        usages = _usages(live)
        order = _order(_mode(live), usages, now)
        # Legacy intent files predate the target field (Task 3); they carry
        # target="" and still belong to whichever task's issue they name —
        # i.get("target") in ("", target) keeps them showing up here instead
        # of vanishing.
        pending = [i for i in sources.pending_intents()
                   if i.get("issue") == issue
                   and i.get("target", "") in ("", target)
                   and i.get("repo", "") in ("", t.repo)
                   and i.get("action") in ("reply", "resume")]
        policy = _policy(live, t.target, t.repo)
        return read_model.task_detail(
            t, model=_model_for(live, t, order, usages, now),
            pinned_track=_pinned_track(live, t),
            pane_tail=sources.pane_tail(target, issue, t.repo),
            session_alive=sources.session_alive(target, issue, t.repo),
            signal=sources.stage_signal(t.worktree),
            events=_events_of(t),
            now=now,
            messages=sources.messages(target, issue, t.repo),
            pending_sends=pending,
            wake_blocked=(target, issue) in sources.wake_blocked_issues(),
            admission=_task_admission(live, t, order, usages, now),
            track_when=(policy.tracks[t.track].when
                       if t.track in policy.tracks else ""))

    def _events_of(t):
        """The log lines of t's repository. An older line names none: it is
        t's only while no task of another repository shares t's number."""
        shared = sources.shares_number(t.target, t.issue, t.repo)
        return [e for e in sources.events_tail(EVENTS_SCAN_LIMIT)
                if e.get("repo", "") == t.repo or (not shared and not e.get("repo", ""))]

    @task_routes(app, "get", "/description",
                 response_model=read_model.IssueDescription)
    def task_description(ref: TaskRef = REF,
                         op: Operator = Depends(current_operator)):
        target, issue, repo = ref
        # `target` is already given, so — unlike the old issue-only route —
        # there is no cross-target AMBIGUITY left to resolve or 409 on: the
        # repo comes straight from this target's config, never scanned for
        # across boot.targets. But the issue must still be real on this
        # target: either a claimed task (state) or a ghost still on this
        # target's own queue (rank_rows) — anything else is a true 404, the
        # same "never a blank, always an explicit state" contract the
        # sibling routes hold to.
        tgt = targets_by_name.get(target)
        if tgt is None:
            raise HTTPException(404, f"unknown target {target!r}")
        task = _resolve(ref) or sources.task(*ref)
        rows = [r for r in _rank_rows(tgt)[0] if r["number"] == issue
                and repo in ("", r.get("repo", ""))]
        if task is None and len(rows) > 1:
            raise ambiguous(target, issue, [r.get("repo", "") for r in rows])
        if task is None and not rows:
            raise HTTPException(404, f"issue {target}/{issue} is neither a "
                                     "task nor on this target's queue")
        named = task.repo if task else rows[0].get("repo", "")
        return read_model.IssueDescription(
            **sources.issue_description(named or repo or tgt.repo, issue))

    def _readable_or_unavailable(
        p: Path, rel: str
    ) -> read_model.ReadableContent | read_model.UnavailableContent:
        """Try to read p as UTF-8; return ReadableContent or UnavailableContent.
        File-missing or non-UTF-8 → UnavailableContent; unexpected exceptions propagate."""
        if not p.is_file():
            return read_model.UnavailableContent(path=rel, reason="file-missing")
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return read_model.UnavailableContent(path=rel, reason="not-utf8")
        except OSError:
            return read_model.UnavailableContent(path=rel, reason="file-missing")
        return read_model.ReadableContent(
            path=rel, media_type=read_model.media_type_for(rel), text=text)

    def _resolve_content(
        raw_path: str, wt: Path
    ) -> read_model.ReadableContent | read_model.UnavailableContent:
        """Resolve raw_path (absolute or worktree-relative) against wt;
        containment guard runs once; delegates read to _readable_or_unavailable."""
        if not raw_path:
            return read_model.UnavailableContent(path="", reason="file-missing")
        sp = Path(raw_path)
        p = (sp if sp.is_absolute() else wt / raw_path).resolve()
        if not p.is_relative_to(wt):
            return read_model.UnavailableContent(
                path=raw_path, reason="path-escapes-worktree")
        return _readable_or_unavailable(p, str(p.relative_to(wt)))

    @task_routes(app, "get", "/request",
                 response_model=read_model.OperatorRequest | None)
    def task_request(ref: TaskRef = REF,
                     op: Operator = Depends(current_operator)):
        t = _find_task(ref)
        req = t.operator_request
        if req is None:
            return None
        wt = Path(t.worktree).resolve()
        # Both request kinds are one file in the worktree: the plan review
        # summary, or the questionnaire.
        return read_model.OperatorRequest(
            kind=req.kind, content=_resolve_content(req.path, wt),
            revision=req.fingerprint, answers=_saved_answers(t))

    def _saved_answers(t) -> read_model.Answers:
        """The answers the page is restored with: those of the intent the
        drain would apply next (made on the request's revision), else the
        answers file's. A whole set, never
        merged: a note cleared in the newer set stays cleared."""
        kind = t.operator_request.kind
        pending = [intents.Intent(i["action"], i["target"], i["issue"],
                                  i["payload"], i["actor"], i["created_at"],
                                  Path(i["id"]), i.get("repo", ""))
                   for i in sources.pending_intents()
                   if (i["action"], i["target"], i["issue"], i.get("repo", ""))
                   == ("answers", t.target, t.issue, t.repo)
                   and isinstance(i["payload"], dict)
                   and isinstance(i["payload"].get("answers"), dict)]
        win = answers_file.select(pending, t.operator_request.fingerprint)
        saved = (win.payload if win is not None
                 else answers_file.read(t.worktree, kind) or {}).get("answers")
        try:
            return _ANSWERS.validate_python(saved)
        except ValidationError:   # a session wrote the file: anything goes
            return {}

    HISTORY_MAX_LINES = 10000

    @task_routes(app, "get", "/history", response_model=read_model.PaneHistory)
    def task_history(lines: int = 2000, ref: TaskRef = REF,
                     op: Operator = Depends(current_operator)):
        t = _find_task(ref)
        # Unbounded `lines` would let one request pull an entire pane history
        # into memory and over the wire; clamp before hitting the session layer.
        clamped = max(1, min(lines, HISTORY_MAX_LINES))
        return read_model.PaneHistory(
            text=sources.pane_history(t.target, t.issue, clamped, t.repo))

    @app.get("/api/usage", response_model=read_model.UsageView)
    def usage_route(op: Operator = Depends(current_operator)):
        live = _cfg()
        return _usage_view(live, _usages(live), datetime.now(timezone.utc), _mode(live))

    @app.post("/api/priority")
    def set_priority(req: PriorityReq,
                     op: Operator = Depends(current_operator)):
        routed = routed_providers(_cfg())
        if req.mode != priority.AUTO and req.mode not in routed:
            raise HTTPException(
                422, f"mode {req.mode!r} is neither {priority.AUTO!r} nor a "
                     f"routed provider ({', '.join(sorted(routed))})")
        priority.save(boot.state_dir, req.mode, actor=op.login,
                      now=datetime.now(timezone.utc))
        # After the file: the event log is the history of what was stored.
        sources.append_event("priority-mode-set", actor=op.login,
                             detail=f"mode={req.mode}")
        return {"ok": True, "mode": req.mode}

    @contextmanager
    def _settings_locked():
        """A save or reset never runs during a dispatcher pass nor during
        another save (flock excludes each open() of the lock file): the
        checks, the file and the next pass always agree."""
        try:
            with pass_lock(boot.state_dir, timeout=PASS_LOCK_WAIT_SECONDS):
                yield _cfg()
        except TimeoutError:
            raise HTTPException(409, "a dispatcher pass or an update is running; "
                                     "try again in a minute") from None

    def _refuse_stale(live: Config, revision: str) -> None:
        if revision != live.models_origin.revision:
            raise HTTPException(409, "the model settings changed since this page "
                                     "loaded them; reload the page and edit again")

    def _refuse_stranding(candidate: Config) -> None:
        """422 when `candidate` would leave an unfinished task unable to go
        on (models.stranded at its next launch; stricter than the
        dispatcher's park, see there)."""
        stranded_tasks = [
            f"{t.target}#{t.issue} ({why})" for t in sources.tasks()
            if t.stage not in TERMINAL_STAGES and t.target in targets_by_name
            and (why := stranded(candidate, _target(t.target, t.repo), t,
                                 next_stage(t)))]
        if stranded_tasks:
            raise HTTPException(422, "unfinished tasks could not go on: "
                                f"{', '.join(stranded_tasks)}")

    def _write(action, op: Operator, event: str) -> msv.ModelSettingsView:
        try:
            action()
        except OSError as e:
            raise HTTPException(500, f"cannot write the model settings: {e}") from e
        # After the file: the event log is the history of what was stored.
        sources.append_event(event, actor=op.login)
        return msv.view(_cfg(), boot.models)

    @app.get("/api/settings/models", response_model=msv.ModelSettingsView)
    def get_model_settings(op: Operator = Depends(current_operator)):
        return msv.view(_cfg(), boot.models)

    @app.put("/api/settings/models", response_model=msv.ModelSettingsView)
    def put_model_settings(req: msv.ModelsSave, op: Operator = Depends(current_operator)):
        with _settings_locked() as live:
            _refuse_stale(live, req.revision)
            try:
                raw = req.models.to_raw()
                candidate = with_models(boot, raw)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            _refuse_stranding(candidate)
            return _write(lambda: model_settings.save(
                boot.state_dir, raw, actor=op.login, now=datetime.now(timezone.utc)),
                op, "model-settings-set")

    @app.delete("/api/settings/models", response_model=msv.ModelSettingsView)
    def reset_model_settings(revision: str, op: Operator = Depends(current_operator)):
        with _settings_locked() as live:
            _refuse_stale(live, revision)
            _refuse_stranding(boot)
            return _write(lambda: model_settings.clear(boot.state_dir), op, "model-settings-reset")

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

    def _locate_row(issue: int, repo: str = ""):
        """Find the row a queue write targets, refusing stale-backed writes.

        When GitHub is unreachable the rank cache keeps the VIEW useful, but
        a write would evaluate a row whose real state may have moved on and
        then push a synchronous GitHub change on that basis.  A cold cache
        additionally yields no rows at all, which must report staleness
        rather than masquerade as an unknown issue.
        """
        hits = []
        stale_as_of = None
        for target in boot.targets:
            rows, as_of, stale = sources.rank_rows(target, fresh=True)
            if stale and stale_as_of is None:
                stale_as_of = as_of
            hits += [(target, row, stale, as_of)
                     for row in rows if row["number"] == issue
                     and repo in ("", row.get("repo", ""))]
        if not hits:
            if stale_as_of is not None:
                raise _stale(stale_as_of)
            raise HTTPException(404, f"issue {issue} not on any queue")
        if len(hits) > 1:
            raise ambiguous(
                "any target", issue, [row.get("repo", "") for _, row, *_ in hits])
        target, row, stale, as_of = hits[0]
        if stale:
            raise _stale(as_of)
        return target, row

    def _apply(event: str, issue: int, plan, op: Operator, detail: str,
               target, row):
        if not plan.ok:
            raise HTTPException(422, plan.reason)
        repo = row.get("repo", "")
        sources.apply_queue_plan(target, issue, plan, repo)
        sources.append_event(event, target=target.name, issue=issue,
                             actor=op.login, detail=detail, repo=repo)
        return {"ok": True, "reason": plan.reason}

    @app.post("/api/queue/boost")
    def queue_boost(req: BoostReq,
                    op: Operator = Depends(current_operator)):
        target, row = _locate_row(req.issue, req.repo)
        return _apply("queue-boost", req.issue,
                      queue_ops.plan_boost(row, req.amount), op,
                      f"amount={req.amount}", target, row)

    @app.post("/api/queue/next")
    def queue_next(req: NextReq,
                   op: Operator = Depends(current_operator)):
        target, row = _locate_row(req.issue, req.repo)
        return _apply("queue-next", req.issue,
                      queue_ops.plan_next(row, req.force), op,
                      f"force={req.force}", target, row)

    @app.post("/api/queue/ready")
    def queue_ready(req: ReadyReq,
                    op: Operator = Depends(current_operator)):
        target, row = _locate_row(req.issue, req.repo)
        return _apply("queue-ready", req.issue,
                      queue_ops.plan_ready(row), op, "", target, row)

    @app.get("/api/queue", response_model=read_model.QueueView)
    def queue_view(op: Operator = Depends(current_operator)):
        tasks = sources.tasks()
        return read_model.QueueView(targets=[
            read_model.target_queue(
                target.name, *sources.rank_rows(target),
                in_flight={(t.repo, t.issue) for t in tasks
                           if t.target == target.name})
            for target in boot.targets])

    def _require_task(ref: TaskRef):
        task = _resolve(ref)
        if task is None:
            raise HTTPException(404, f"no task {ref.target}/{ref.issue}")
        return task

    def _accepted(action: str, ref: TaskRef, payload: dict, op: Operator):
        name = sources.submit_intent(action, ref.target, ref.issue, payload,
                                     op.login, ref.repo)
        return JSONResponse({"status": "pending", "intent": name},
                            status_code=202)

    def _backlog_rows(ref: TaskRef) -> list:
        """The queue rows `ref` names on its target."""
        target = targets_by_name.get(ref.target)
        return [] if target is None else [
            row for row in _rank_rows(target)[0]
            if row["number"] == ref.issue and ref.repo in ("", row.get("repo", ""))]

    def _named(ref: TaskRef) -> TaskRef:
        """`ref` with the repository of the task or queue row it names, when
        there is one: an old reference reaches the one item of that number,
        409 when several."""
        task = _resolve(ref)
        rows = _backlog_rows(ref)
        archived = sources.archived_named(ref.target, ref.issue) if not ref.repo else []
        repos = list(dict.fromkeys(
            [t.repo for t in archived] + ([task.repo] if task is not None else [])
            + [row.get("repo", "") for row in rows]))
        if len(repos) > 1:
            raise ambiguous(ref.target, ref.issue, repos)
        if task is not None:
            return TaskRef(*ref[:2], task.repo)
        return TaskRef(*ref[:2], repos[0]) if repos else ref

    @task_routes(app, "post", "/reply", status_code=202)
    def intent_reply(req: ReplyReq, ref: TaskRef = REF,
                     op: Operator = Depends(current_operator)):
        # NO _require_task: the contract is "never drop a message", and that
        # includes an unclaimed backlog issue (a pre-briefing delivered at
        # claim time) and a done/failed tombstone (delivered if it restarts).
        # The queue is keyed by issue and survives every task lifecycle, so
        # there is nothing here to 404 against.
        return _accepted("reply", _named(ref), {"text": req.text}, op)

    @task_routes(app, "post", "/answers", status_code=202,
                 dependencies=[Depends(_capped_body)])
    def intent_answers(req: AnswersReq, ref: TaskRef = REF,
                       op: Operator = Depends(current_operator)):
        task = _require_task(ref)
        return _accepted("answers", TaskRef(*ref[:2], task.repo),
                         req.model_dump(mode="json"), op)

    @task_routes(app, "post", "/park", status_code=202)
    def intent_park(ref: TaskRef = REF,
                    op: Operator = Depends(current_operator)):
        return _accepted("park", TaskRef(*ref[:2], _require_task(ref).repo), {}, op)

    @task_routes(app, "post", "/kill", status_code=202)
    def intent_kill(ref: TaskRef = REF,
                    op: Operator = Depends(current_operator)):
        return _accepted("kill", TaskRef(*ref[:2], _require_task(ref).repo), {}, op)

    @task_routes(app, "post", "/cancel", status_code=202)
    def intent_cancel(ref: TaskRef = REF,
                      op: Operator = Depends(current_operator)):
        # NO _require_task: a backlog card with no task file cancels too —
        # the target in the path lets the dispatcher retire the board card.
        named = _named(ref)
        if _resolve(named) is None and not _backlog_rows(named):
            raise HTTPException(404, f"no task or queue issue {ref.target}/{ref.issue}")
        return _accepted("cancel", named, {}, op)

    @task_routes(app, "post", "/retry", status_code=202)
    def intent_retry(ref: TaskRef = REF,
                     op: Operator = Depends(current_operator)):
        repos = [q.get("repo", "") for q in sources.quarantine_entries()
                 if (q.get("target"), q.get("task_issue")) == (ref.target, ref.issue)
                 and ref.repo in ("", q.get("repo", ""))]
        if len(repos) > 1:
            raise ambiguous(ref.target, ref.issue, repos)
        if not repos:
            raise HTTPException(
                404, f"issue {ref.target}/{ref.issue} is not quarantined")
        return _accepted("retry", TaskRef(*ref[:2], repos[0]), {}, op)

    @task_routes(app, "post", "/resume", status_code=202)
    def intent_resume(req: ResumeReq, ref: TaskRef = REF,
                      op: Operator = Depends(current_operator)):
        task = _find_task(ref)
        target, issue = task.target, task.issue
        if task.stage in TERMINAL_STAGES and not resumable_crash(task):
            # The dispatcher would drop it; say so instead of queueing it.
            raise HTTPException(409, f"task {target}/{issue} is {task.stage.value} "
                                "and cannot be resumed")
        configured_target = _target(task.target, task.repo)
        policy = policy_for(_cfg(), configured_target)
        configured = policy.model_ids()
        if req.model and req.model not in configured:
            raise HTTPException(422, f"model {req.model!r} is not configured "
                                f"for target {target!r}")
        _require_same_provider(task, req.model)
        payload = {"text": req.text} if req.text else {}
        if req.model:
            payload["model"] = req.model
        if req.bypass_usage:
            payload["bypass_usage"] = True
        return _accepted("resume", TaskRef(target, issue, task.repo), payload, op)

    def _queue_candidate(configured_target, ref: TaskRef):
        rows, _as_of, stale = _rank_rows(configured_target)
        found = [candidate for candidate in rows
                 if candidate.get("number") == ref.issue
                 and ref.repo in ("", candidate.get("repo", ""))
                 and read_model._is_candidate(candidate)]
        if len(found) > 1:
            raise ambiguous(ref.target, ref.issue, [c.get("repo", "") for c in found])
        if found:
            return found[0]
        detail = (f"queue for {ref.target!r} is stale"
                  if stale else f"no runnable task {ref.target}/{ref.issue}")
        raise HTTPException(409 if stale else 404, detail)

    def _runnable_work(configured_target, ref: TaskRef):
        task = _resolve(ref)
        if task is None:
            return None, _queue_candidate(configured_target, ref)
        if task.stage in TERMINAL_STAGES:
            raise HTTPException(422, f"task {ref.target}/{ref.issue} is terminal")
        return task, None

    def _run_model(configured_target, target: str, req: RunReq, task, row):
        live = _cfg()
        policy = policy_for(live, configured_target)
        if (req.model and req.model not in policy.model_ids()
                and req.model not in {str(e) for e in policy.entries()}):
            raise HTTPException(422, f"model {req.model!r} is not configured "
                                f"for target {target!r}")
        if task is not None:
            _require_same_provider(task, req.model)
        if req.model:
            return req.model
        if task is not None:
            # No pick: the model the card names, the first admitted entry.
            now, usages = datetime.now(timezone.utc), _usages(live)
            return (stage_pick(task.picks, next_stage(task))
                    or _model_for(live, task, _order(_mode(live), usages, now), usages, now))
        return _candidate_model(live, configured_target, row, _order(_mode(live)))

    def _arm_run(ref: TaskRef, req: RunReq, task, model: str, op: Operator):
        if task is not None and task.park:
            payload = {"model": model}
            if req.bypass_usage:
                payload["bypass_usage"] = True
            return _accepted("resume", ref, payload, op)
        sources.set_execution_override(
            ref.target, ref.issue, model=model, bypass_usage=req.bypass_usage,
            repo=ref.repo)
        sources.append_event("execution-forced", target=ref.target,
                             issue=ref.issue, actor=op.login,
                             detail=f"model={model}", repo=ref.repo)
        return {"ok": True, "reason": f"#{ref.issue} will run with {model}"}

    @task_routes(app, "post", "/run")
    def force_run(req: RunReq, ref: TaskRef = REF,
                  op: Operator = Depends(current_operator)):
        configured_target = targets_by_name.get(ref.target)
        if configured_target is None:
            raise HTTPException(404, f"unknown target {ref.target!r}")
        # Name the identity first: archived, active and every matching queue
        # row, runnable or not. Eligibility must never pick among them.
        task, row = _runnable_work(configured_target, _named(ref))
        repo = task.repo if task is not None else row.get("repo", "")
        model = _run_model(_target(ref.target, repo), ref.target, req, task, row)
        return _arm_run(TaskRef(ref.target, ref.issue, repo), req, task, model, op)

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
