"""Project onboarding: the engine of the agent-ops-onboard unit.

`agent-ops project add` (dispatcher.onboard_cli) writes one intent file per
action into state_dir/onboarding-intents/ and starts the unit. Each run takes
onboarding.lock, applies then deletes the intents in lexical order, and works
the first unverified step of every open record in state_dir/onboarding/,
saving after each step and stopping a record at its first step that isn't
verified. Blocked and failed steps are retried on every run; a timer reruns
the unit for the long waits. Only this module writes the records, and their
layout is private to it.

Every step returns an Outcome; an exception it doesn't expect fails it.
Every subprocess goes through `sh(argv, cwd=None, env=None) -> str` (raises
CalledProcessError) and every HTTP request through `get(url, headers) ->
bytes`: the two seams tests fake."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal, NamedTuple

import yaml

from dispatcher import board, config, intents, labels, project_board, repository_inspection as inspection
from dispatcher.infra_config import add_project_text, project_block
from dispatcher.pr_poll import ci_verdict
from dispatcher.state import state_lock, write_json_atomic

CHECKOUT = Path(__file__).resolve().parents[1]   # the executing agent-ops checkout

RECORDS_DIR = "onboarding"
INFRA_REPO = "jesdi/agent-ops-infra"
INFRA_TARGETS = "config/targets.yaml"
# git authenticates as gh does, so GH_TOKEN in the env picks the token.
GIT_AS_GH = ["git", "-c", "credential.helper=", "-c",
             "credential.helper=!gh auth git-credential"]

Sh = Callable[..., str]
Get = Callable[[str, dict], bytes]


def sh(argv: list[str], cwd: str | None = None, env: dict | None = None) -> str:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                          check=True, timeout=1800).stdout


def get(url: str, headers: dict) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                timeout=10) as resp:
        return resp.read()


@dataclass(frozen=True)
class Run:
    config_path: Path
    cfg: config.Config
    sh: Sh
    get: Get
    now: datetime
    _builder: list = field(default_factory=list)   # the run's builder, once read
    adopted: set = field(default_factory=set)      # projects whose head this run adopted


def _builder(r: Run) -> str:
    """The executing agent-ops checkout's revision, read once per run whatever
    the cwd; an unreadable or empty one fails the step (retried next run)."""
    if not r._builder:
        r._builder.append(r.sh(["git", "-C", str(CHECKOUT), "rev-parse", "HEAD"]).strip())
    if not r._builder[0]:
        r._builder.clear()
        raise RuntimeError("the running agent-ops revision is unknown")
    return r._builder[0]


# -- the record ---------------------------------------------------------------


class StepState(StrEnum):
    PENDING = "pending"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    VERIFIED = "verified"


@dataclass(frozen=True)
class Outcome:
    """What a step found. Blocked: the operator must act, `detail` names the
    action and `link` points at it."""
    state: StepState
    detail: str = ""
    link: str = ""


VERIFIED = Outcome(StepState.VERIFIED)


def blocked(detail: str, link: str = "") -> Outcome:
    return Outcome(StepState.BLOCKED, detail, link)


def waiting(detail: str, link: str = "") -> Outcome:
    return Outcome(StepState.WAITING, detail, link)


@dataclass
class Step:
    name: str
    state: StepState = StepState.PENDING
    detail: str = ""
    link: str = ""
    since: str = ""


@dataclass
class InfraPr:
    number: int
    url: str
    head: str
    base: str
    revision: str
    pushed_at: str
    builder: str = ""      # the agent-ops revision that pushed it; "" is unknown


@dataclass
class Record:
    project: str
    status: str = "open"                 # open | complete
    started_by: str = ""
    updated_at: str = ""
    request: dict = field(default_factory=dict)
    inspection: inspection.Inspection = field(default_factory=inspection.Inspection)
    confirm_ready: bool = False
    proposal: dict = field(default_factory=dict)    # the targets.yaml block
    revision: str = ""
    confirmed_revision: str = ""
    notice: str = ""
    steps: list[Step] = field(default_factory=lambda: [Step(n) for n in STEPS])
    infra_pr: InfraPr | None = None
    merge_sha: str = ""                  # the infra main commit that holds the project
    merged_at: str = ""
    resources: list[dict] = field(default_factory=list)
    deployed_at: str = ""
    awaiting: str = ""                   # "confirm" while the operator must confirm
    board_number: int = 0                # the board the board step created
    board_review: list[str] = field(default_factory=list)  # what the board step will add

    @property
    def pr_url(self) -> str:
        return self.infra_pr.url if self.infra_pr else ""

    def step(self, name: str) -> Step:
        return next(s for s in self.steps if s.name == name)

    @classmethod
    def from_json(cls, d: dict) -> Record:
        ip = d.get("infra_pr")
        return cls(**{**d, "steps": _with_board_step(d),
                      "infra_pr": InfraPr(**ip) if ip else None,
                      "inspection": inspection.Inspection.from_json(d.get("inspection", {}))})


def _with_board_step(d: dict) -> list[Step]:
    """The record's steps; one from before the board step gains it before
    config-pr, verified once the record is complete."""
    steps = [Step(**{**s, "state": StepState(s["state"])}) for s in d["steps"]]
    if all(s.name != "board" for s in steps):
        at = next(i for i, s in enumerate(steps) if s.name == "config-pr")
        steps.insert(at, Step("board", StepState.VERIFIED if d.get("status") == "complete"
                              else StepState.PENDING))
    return steps


def _record_path(state_dir: str | Path, project: str) -> Path:
    return Path(state_dir) / RECORDS_DIR / f"{project}.json"


def load_record(state_dir: str | Path, project: str) -> Record | None:
    p = _record_path(state_dir, project)
    return Record.from_json(json.loads(p.read_text())) if p.exists() else None


def list_records(state_dir: str | Path) -> list[Record]:
    return [Record.from_json(json.loads(p.read_text()))
            for p in sorted((Path(state_dir) / RECORDS_DIR).glob("*.json"))]


def _save(r: Run, rec: Record) -> None:
    rec.updated_at = r.now.isoformat()
    rec.awaiting = "confirm" if rec.step("confirm").state is StepState.BLOCKED else ""
    write_json_atomic(_record_path(r.cfg.state_dir, rec.project), asdict(rec))


def revision(proposal: dict) -> str:
    return hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()


# -- intents ------------------------------------------------------------------

QUEUE = intents.Queue("onboarding-intents", ("add", "confirm", "resume", "skip-inspection"), ("project",))


class Intent(NamedTuple):
    action: str
    project: str
    payload: dict    # validated: a request, a revision or nothing
    actor: str
    path: Path


def write_intent(state_dir: str | Path, action: str, project: str, payload: dict,
                 actor: str, epoch_ms: int) -> Path:
    return QUEUE.write(state_dir, action, (project,), payload, actor, epoch_ms)


def pending_intents(state_dir: str | Path) -> list[dict]:
    """Read valid queue entries without consuming or repairing them."""
    return QUEUE.inspect(state_dir, _pending_intent)


def _pending_intent(raw: dict, path: Path) -> dict:
    it = _intent(raw, path)
    return {"id": path.name, "action": it.action, "project": it.project,
            "payload": it.payload, "actor": it.actor,
            "created_at": str(raw.get("created_at", ""))}


def _intent(rec: dict, path: Path) -> Intent:
    action, project, payload = rec["action"], rec["project"], rec["payload"]
    if action not in QUEUE.actions or not config._SLUG.fullmatch(project):
        raise ValueError(f"action {action!r} for project {project!r}")
    payload = _intent_payload(action, payload)
    return Intent(action, project, payload, str(rec.get("actor", "")), path)


def _intent_payload(action: str, payload: dict) -> dict:
    if action == "add":
        request = _request(payload)
        if "expected_request_revision" in payload:
            request["expected_request_revision"] = str(payload["expected_request_revision"])
        return request
    if action in ("confirm", "skip-inspection"):
        return {"revision": str(payload["revision"])}
    return {}


def _request(payload: dict) -> dict:
    """The validated repos, board and per-repository commands of an add."""
    repos = payload["repos"]
    board_value = payload.get("board")
    commands = payload.get("commands", [{} for _ in repos])
    if not repos or len(commands) != len(repos):
        raise ValueError("repos must be non-empty, with one commands entry each")
    automatic = payload.get("inspect", False)
    result = {"repos": [str(x) for x in repos],
              "board": _board_request(board_value),
              "commands": [_command_request(c, automatic) for c in commands]}
    if "inspect" in payload:
        result["inspect"] = bool(automatic)
    return result


def _board_request(value: dict | None) -> dict | None:
    """owner/number of an existing board, or a new one."""
    if not value:
        return None
    if value.get("new") is True:
        return {"new": True}
    return {"owner": str(value["owner"]), "number": int(value["number"])}


def _board_choice(chosen: dict | None, project: str, first_repo: str) -> dict | None:
    """The proposal's board: a new one is "<project> Backlog", owned by the
    first repository's owner."""
    if chosen and chosen.get("new"):
        return {"owner": first_repo.split("/")[0], "new": True, "title": f"{project} Backlog"}
    return chosen


def _command_request(raw: dict, automatic: bool) -> dict:
    result = {key: (None if automatic and raw.get(key) is None else str(raw.get(key) or ""))
              for key in inspection.COMMANDS}
    if "services" in raw:
        result["services"] = raw["services"]
    return result


def _drain(r: Run) -> None:
    """Apply then delete every intent, in lexical order: at most once."""
    for it in QUEUE.read(r.cfg.state_dir, _intent):
        _APPLY[it.action](r, it)
        it.path.unlink(missing_ok=True)


def request_revision(request: dict) -> str:
    """Version of the operator's request, independent of resolved proposal facts."""
    return revision({**_request(request), "repos": _repo_keys(request)})


def request_conflict(rec: Record | None, request: dict, expected: str | None) -> str:
    """Absent precondition is the legacy CLI; empty means web create-only."""
    if expected is None:
        return ""
    if rec is None:
        return "request refused: onboarding no longer exists" if expected else ""
    if _same(rec.request, request):
        return ""
    if expected != request_revision(rec.request):
        return "request refused: details changed; review the latest details before saving again"
    return ""


def _add(r: Run, it: Intent) -> None:
    """Apply the request's precondition and transition policy under the engine lock."""
    rec = load_record(r.cfg.state_dir, it.project)
    request = _request(it.payload)
    conflict = request_conflict(rec, request, it.payload.get("expected_request_revision"))
    if conflict:
        _refuse_request(r, rec, conflict)
        return
    if rec is None:
        rec = Record(it.project, started_by=it.actor, request=request)
    else:
        rec.notice = _rerun(r, rec, request)
    _save(r, rec)


def _refuse_request(r: Run, rec: Record | None, reason: str) -> None:
    if rec is None:
        print(f"[warn] {reason}; dropped", file=sys.stderr)
        return
    rec.notice = reason
    _save(r, rec)


EditMode = Literal["none", "details", "repositories"]


def edit_mode(rec: Record) -> EditMode:
    """The engine's editing policy, also shown by the console."""
    if rec.status != "open" or rec.step("config-merge").state is StepState.VERIFIED:
        return "none"
    return "details" if rec.confirmed_revision else "repositories"


def _rerun(r: Run, rec: Record, request: dict) -> str:
    """Apply a request to an existing record; the notice it leaves. The same
    request resumes. Before the merge, another one is resolved again from
    the first step and so goes back to confirm, but after confirm its
    repositories can't change."""
    if _same(rec.request, request):
        return "already onboarded" if rec.status == "complete" else ""
    mode = edit_mode(rec)
    if mode == "none":
        return ("request refused: the project is merged into infra; edit "
                "targets.yaml by hand for now")
    if mode == "details" and _repo_keys(rec.request) != _repo_keys(request):
        return "request refused: the repositories can't change after confirm"
    rec.confirm_ready = False
    rec.inspection.generation += 1
    rec.request, rec.steps = request, [Step(n) for n in STEPS]
    return ""


def _retain_inspection(r: Run, rec: Record, resolved: list[dict]) -> None:
    """Reconcile only after GitHub resolves spellings to stable repository IDs."""
    from dispatcher import onboarding_sessions
    repositories = {x["github_id"]: x for x in resolved}
    reports = rec.inspection.repositories
    for report in reports:
        if report.github_id in repositories:
            report.repo = repositories[report.github_id]["repo"]
        elif report.request_id:
            onboarding_sessions.withdraw(r.cfg.state_dir, report.request_id)
    rec.inspection.repositories = [x for x in reports if x.github_id in repositories]


def repo_key(spelling: str) -> str:
    """One key for every spelling of a repository."""
    return (slug(spelling) or spelling).lower()


def _repo_keys(request: dict) -> list[str]:
    return [repo_key(x) for x in request["repos"]]


def _same(a: dict, b: dict) -> bool:
    return request_revision(a) == request_revision(b)


def _confirm(r: Run, it: Intent) -> None:
    """Confirm names the revision it saw; a stale one is refused, as the
    console refuses stale model settings."""
    rec = load_record(r.cfg.state_dir, it.project)
    if rec is None or rec.status != "open":
        print(f"[warn] confirm for {it.project}: no open onboarding; dropped",
              file=sys.stderr)
        return
    seen = it.payload["revision"]
    if seen == rec.revision and can_confirm(rec):
        rec.confirmed_revision, rec.notice = seen, ""
    else:
        rec.notice = (f"confirm of revision {seen[:12]} refused: the proposal is "
                      f"now revision {rec.revision[:12]}; review it and confirm again")
    _save(r, rec)


def _resume(r: Run, it: Intent) -> None:
    """Retry the latest record, never a frontend's snapshot of its request."""
    rec = load_record(r.cfg.state_dir, it.project)
    if rec is not None and rec.status == "open":
        rec.notice = ""
        _save(r, rec)


def _skip_inspection(r: Run, it: Intent) -> None:
    rec = load_record(r.cfg.state_dir, it.project)
    if rec is None or rec.status != "open":
        return
    if rec.revision != it.payload["revision"]:
        rec.notice = "proposal changed; review it before filling in by hand"
    else:
        rec.confirm_ready = False
        rec.inspection.manual = True
        for report in rec.inspection.repositories:
            if report.state == "waiting":
                _skip_report(r.cfg.state_dir, report)
        rec.notice = ""
    _save(r, rec)


_APPLY = {"add": _add, "confirm": _confirm, "resume": _resume,
          "skip-inspection": _skip_inspection}


# -- the run ------------------------------------------------------------------


def run(config_path: str | Path, sh: Sh = sh, get: Get = get,
        now: datetime | None = None) -> None:
    cfg = config.load_config(config_path)
    r = Run(Path(config_path), cfg, sh, get, now or datetime.now(timezone.utc))
    with state_lock(cfg.state_dir, "onboarding.lock"):
        _drain(r)
        for rec in list_records(cfg.state_dir):
            if rec.status == "open":
                _advance(r, rec)


def _advance(r: Run, rec: Record) -> None:
    """Work the record's steps in order; stop at the first not verified."""
    for s in rec.steps:
        if s.state is StepState.VERIFIED:
            continue
        out = _work(r, rec, s.name)
        if out.state != s.state:
            s.since = r.now.isoformat()
        s.state, s.detail, s.link = out.state, out.detail, out.link
        _save(r, rec)
        if out.state is not StepState.VERIFIED:
            return
    rec.status = "complete"
    _save(r, rec)


def _work(r: Run, rec: Record, name: str) -> Outcome:
    try:
        return _STEPS[name](r, rec)
    except Exception as e:  # any other error fails the step; every run retries it
        return Outcome(StepState.FAILED,
                       (getattr(e, "stderr", None) or repr(e)).strip()[-2000:])


# -- resolve ------------------------------------------------------------------

_GITHUB = re.compile(r"(?:https://github\.com/|(?:ssh://)?git@github\.com[:/])?"
                     r"([\w.-]+/[\w.-]+?)(?:\.git)?/?")


def slug(spelling: str) -> str | None:
    """"owner/name" of a GitHub repository spelt as a slug, an https or ssh
    URL, with or without .git; None when it is none of these."""
    m = _GITHUB.fullmatch(spelling.strip())
    return m.group(1) if m else None


def _resolve(r: Run, rec: Record) -> Outcome:
    found = []
    for spelling in rec.request["repos"]:
        x = _github_repo(r, spelling)
        if isinstance(x, Outcome):
            return x
        found.append(x)
    proposal = _proposal(rec, found)
    if out := _held(r, rec.project, found):
        return out
    if "inspect" not in rec.request:
        if out := (_check_board(r, rec, proposal) or _check_fits(r, proposal)):
            return out
    _retain_inspection(r, rec, found if "inspect" in rec.request else [])
    rec.proposal, rec.revision = proposal, revision(proposal)
    return VERIFIED


def _github_repo(r: Run, spelling: str) -> dict | Outcome:
    s = slug(spelling)
    if not s:
        return blocked(f"{spelling!r} is not a GitHub repository (owner/name or its URL)")
    try:
        d = json.loads(r.sh(["gh", "api", f"repos/{s}"]))
    except subprocess.CalledProcessError as e:
        if "404" not in (e.stderr or ""):
            raise
        return blocked(f"{s}: no such repository on GitHub, or the box's "
                       "token can't read it")
    return {"name": d["name"], "repo": d["full_name"], "github_id": d["id"]}


def _held(r: Run, project: str, found: list[dict]) -> Outcome | None:
    holders = _holders(r, project)
    for x in found:
        if holder := holders.get(x["github_id"]) or holders.get(x["repo"].lower()):
            return blocked(f"{x['repo']} already belongs to project {holder!r}")
    return None


def _holders(r: Run, project: str) -> dict:
    """github_id and lowercased owner/name -> the project holding it: a
    config project or another open record (R1)."""
    held = {}
    for x in r.cfg.repositories:
        held[x.github_id] = held[x.repo.lower()] = x.project.name
    for other in list_records(r.cfg.state_dir):
        if other.status == "open" and other.project != project:
            for x in other.proposal.get("repositories", []):
                held[x["github_id"]] = held[x["repo"].lower()] = other.project
    return held


def _proposal(rec: Record, found: list[dict]) -> dict:
    """The targets.yaml block: paused, paths derived (#226)."""
    root = Path(config.REPOS_ROOT).expanduser() / rec.project
    return {"name": rec.project, "paused": True,
            "board": _board_choice(rec.request["board"], rec.project, found[0]["repo"]),
            "repositories": [{**x, "clone_path": str(root / x["name"]),
                              "worktrees_path": str(root / f"{x['name']}.worktrees"),
                              **{k: v or "" for k, v in commands.items() if k != "services"},
                              **({"services": commands["services"]} if commands.get("services") else {})}
                             for x, commands in zip(found, rec.request["commands"])]}


def _gh(r: Run) -> project_board.Gh:
    return project_board.Gh(board=lambda args: r.sh(["gh", *args], env=board.project_env()),
                            repo=lambda args: r.sh(["gh", *args]))


def _check_board(r: Run, rec: Record, proposal: dict) -> Outcome | None:
    """Refuse a board with a clashing field or a card from outside the
    project; else note in the record what the board step will add, for the
    operator to confirm (#230 D11)."""
    b, repos = proposal["board"], [x["repo"] for x in proposal["repositories"]]
    tracks = labels.tracks(r.cfg)
    if b.get("new"):
        rec.board_review = [f"create board {b['title']!r} for {b['owner']}", *project_board.label_lines(
            project_board.missing_labels(repos, tracks, _gh(r)))]
        return None
    try:
        s = project_board.survey(b["owner"], b["number"], repos, tracks, _gh(r))
    except board.BoardError as e:
        return blocked(str(e))
    if blocker := s.blocker():
        return blocked(blocker, s.fields.url)
    rec.board_review = s.review()
    return None


def _config_proposal(proposal: dict, number: int) -> dict:
    """The targets.yaml block: a new board under the number it got."""
    b = proposal["board"]
    if not (b and b.get("new")):
        return proposal
    return {**proposal, "board": {"owner": b["owner"], "number": number}}   # no "new", no "title"


def _check_fits(r: Run, proposal: dict) -> Outcome | None:
    raw = yaml.safe_load(r.config_path.read_text())
    # A board still to be created checks as number 1: only its shape matters.
    raw["projects"] = [*(raw.get("projects") or []), _config_proposal(proposal, 1)]
    try:
        config.parse_config(raw)
    except ValueError as e:
        return blocked(f"the live config refuses the proposal: {e}")
    return None


# -- clone --------------------------------------------------------------------


def _clone(r: Run, rec: Record) -> Outcome:
    for x in rec.proposal["repositories"]:
        if out := _clone_one(r, Path(x["clone_path"]), x["repo"]):
            return out
        _resource(rec, "clone", x["clone_path"])
    return VERIFIED


def _clone_one(r: Run, path: Path, repo: str) -> Outcome | None:
    """Clone into <path>.partial, then rename: a path that exists is a whole
    clone. The repository at the path must be `repo`."""
    if not path.exists():
        partial = path.with_name(f"{path.name}.partial")
        if out := _clear_partial(r, partial, repo):
            return out
        partial.parent.mkdir(parents=True, exist_ok=True)
        # An explicit https URL: gh's git_protocol may be ssh on the box, which
        # has no GitHub ssh key; git's gh credential helper authenticates https.
        r.sh(["git", "clone", f"https://github.com/{repo}.git", str(partial)])
        partial.rename(path)
    origin = _origin(r, path)
    if origin != repo.lower():
        return blocked(f"{path} holds {origin}, not {repo}: move it away, then resume")
    return None


def _clear_partial(r: Run, partial: Path, repo: str) -> Outcome | None:
    """Remove a stale <path>.partial when it is empty or a clone of `repo`
    a crash interrupted; anything else there blocks."""
    if not partial.exists():
        return None
    if any(partial.iterdir()) and not _partial_clone_of(r, partial, repo):
        return blocked(f"{partial} holds something other than a partial clone of "
                       f"{repo}: move it away, then resume")
    shutil.rmtree(partial)
    return None


def _partial_clone_of(r: Run, partial: Path, repo: str) -> bool:
    if not (partial / ".git").exists():
        return False
    try:
        return _origin(r, partial) == repo.lower()
    except subprocess.CalledProcessError:
        return False


def _origin(r: Run, path: Path) -> str:
    """The lowercased owner/name of the clone at `path`'s origin."""
    url = r.sh(["git", "-C", str(path), "remote", "get-url", "origin"])
    return (slug(url) or url.strip()).lower()


def _resource(rec: Record, kind: str, ref: str) -> None:
    entry = {"kind": kind, "ref": ref, "state": "in use"}
    if entry not in rec.resources:
        rec.resources.append(entry)


# -- inspect ------------------------------------------------------------------


def _inspect(r: Run, rec: Record) -> Outcome:
    if "inspect" not in rec.request:
        return VERIFIED
    for repository in rec.proposal["repositories"]:
        report = _inspection_report(r, rec, repository)
        if report.state == "waiting":
            _inspection_progress(r, report, repository, rec.request["inspect"])
    _apply_inspection(rec)
    rec.revision = revision(rec.proposal)
    return _inspection_outcome(rec.inspection.repositories)


def _inspection_outcome(reports: list[inspection.RepositoryInspection]) -> Outcome:
    pending = [x for x in reports if x.state == "waiting"]
    if pending:
        return waiting(pending[0].detail or "waiting for repository inspection")
    return Outcome(StepState.VERIFIED, "; ".join(x.warning for x in reports if x.error))


def _inspection_board(rec: Record) -> dict | None:
    if rec.request["board"]:
        return _board_choice(rec.request["board"], rec.project, rec.proposal["repositories"][0]["repo"])
    choices = {json.dumps(x.facts.board, sort_keys=True): x.facts.board
               for x in rec.inspection.repositories if x.facts.board}
    return next(iter(choices.values())) if len(choices) == 1 else None


def _inspection_report(r: Run, rec: Record, repository: dict) -> inspection.RepositoryInspection:
    from dispatcher import onboarding_sessions
    for report in rec.inspection.repositories:
        if report.github_id == repository["github_id"]:
            return report
    report = inspection.RepositoryInspection(repository["repo"], inspection.facts(repository["clone_path"]),
                                             github_id=repository["github_id"])
    if rec.inspection.manual or not rec.request["inspect"]:
        report.skip()
    else:
        report.request_id = onboarding_sessions.ensure_request(
            r.cfg.state_dir, rec.project, repository["repo"], request_revision(rec.request),
            repository["clone_path"], asdict(report.facts), generation=rec.inspection.generation)
    rec.inspection.repositories.append(report)
    return report


def _inspection_progress(r: Run, report: inspection.RepositoryInspection, repository: dict, automatic: bool) -> None:
    from dispatcher import onboarding_sessions
    if not automatic:
        _skip_report(r.cfg.state_dir, report)
        return
    current = onboarding_sessions.status(r.cfg.state_dir, report.request_id)
    report.detail = current["detail"]
    if current["state"] in ("complete", "failed", "withdrawn"):
        report.complete(current.get("result"), repository["clone_path"], current["detail"])


def _skip_report(state_dir: str, report: inspection.RepositoryInspection) -> None:
    from dispatcher import onboarding_sessions
    onboarding_sessions.withdraw(state_dir, report.request_id)
    report.skip()


def inspection_view(rec: Record) -> dict:
    """Join overrides to resolved repository IDs, independent of submitted aliases."""
    commands = _resolved_commands(rec)
    return {"repositories": [report.view(commands.get(report.github_id, {}), rec.request["board"])
                             for report in rec.inspection.repositories]}


def _resolved_commands(rec: Record) -> dict[int, dict]:
    if rec.step("resolve").state is not StepState.VERIFIED:
        return {}
    return {repository["github_id"]: command for repository, command in
            zip(rec.proposal.get("repositories", []), rec.request["commands"])}


def _apply_inspection(rec: Record) -> None:
    views = {report["repo"]: report for report in inspection_view(rec)["repositories"]}
    for repository in rec.proposal["repositories"]:
        view = views[repository["repo"]]
        repository.update({key: item["value"] for key, item in view["fields"].items()})
        repository["services"] = [x["value"] for x in view["selected_services"]]
    rec.proposal["board"] = _inspection_board(rec)


# -- confirm ------------------------------------------------------------------


def _requirements_met(rec: Record) -> bool:
    if not rec.proposal.get("board"):
        return False
    if any(not x.get("gate_cmd", "").strip() for x in rec.proposal.get("repositories", [])):
        return False
    if "inspect" in rec.request:
        if len(rec.inspection.repositories) != len(rec.proposal["repositories"]):
            return False
    return not _invalid_services(rec) and all(x.state != "waiting" for x in rec.inspection.repositories)


def can_confirm(rec: Record) -> bool:
    return rec.confirm_ready and _requirements_met(rec)


def _invalid_services(rec: Record) -> str:
    reports = {x.repo: x for x in rec.inspection.repositories}
    for repository in rec.proposal.get("repositories", []):
        report = reports.get(repository["repo"])
        if report is None:
            continue
        found = {x.value for x in report.facts.services}
        invalid = set(repository.get("services", [])) - found
        if invalid:
            return f"{repository['repo']}: services not found in Compose files: {', '.join(sorted(invalid))}"
    return ""


def _confirm_step(r: Run, rec: Record) -> Outcome:
    rec.confirm_ready = False
    if problem := _invalid_services(rec):
        return blocked(problem)
    if not _requirements_met(rec):
        return blocked("choose a board and fill in a gate command for each repository")
    if out := (_check_board(r, rec, rec.proposal) or _check_fits(r, rec.proposal)):
        return out
    rec.confirm_ready = True
    if rec.revision and rec.confirmed_revision == rec.revision:
        return VERIFIED
    b = rec.proposal["board"]
    choice = "new" if b.get("new") else f"{b['owner']}/{b['number']}"
    return blocked(f"review the proposal, then confirm revision {rec.revision[:12]}: "
                   f"agent-ops project add {rec.project} {' '.join(rec.request['repos'])} "
                   f"--board {choice}")


# -- board --------------------------------------------------------------------


def _board(r: Run, rec: Record) -> Outcome:
    """Prepare the confirmed board (#230): recompute from the live board and
    add only what is still missing. A new clash or foreign card blocks here;
    it doesn't go back to confirm (D11)."""
    owner = rec.proposal["board"]["owner"]
    number = rec.proposal["board"].get("number") or _new_board(r, rec)
    if isinstance(number, Outcome):
        return number
    repos, tracks, gh = [x["repo"] for x in rec.proposal["repositories"]], labels.tracks(r.cfg), _gh(r)
    s = project_board.survey(owner, number, repos, tracks, gh)
    _resource(rec, "board", s.fields.url)
    if blocker := s.blocker():
        return blocked(blocker, s.fields.url)
    board.prepare(s.fields, s.gaps, gh.board)
    note = _link(r, s.fields, owner, repos)
    synced = project_board.sync(project_board.survey(owner, number, repos, tracks, gh), tracks, gh)
    s = project_board.survey(owner, number, repos, tracks, gh)
    problems = [*s.gaps.lines(), *project_board.label_lines(s.missing_labels),
                *(f"no card for {i['url']}" for i in s.uncarded() if i["url"] not in synced.cards)]
    if problems:   # fails the step: the next run tries again
        raise RuntimeError(f"board {s.fields.project} after preparing: {'; '.join(problems)}")
    return Outcome(StepState.VERIFIED, note, s.fields.url)


def _new_board(r: Run, rec: Record) -> int | Outcome:
    """The number of the board made for the project: from the record, else
    found by its title (a crash lost the save after the create), else new."""
    if rec.board_number:
        return rec.board_number
    owner, title = rec.proposal["board"]["owner"], rec.proposal["board"]["title"]
    found = board.find(owner, title, _gh(r).board)
    if len(found) > 1:
        return blocked(f"{owner} has {len(found)} boards titled {title!r}: rename all but "
                       "one, then resume")
    if found:   # made by a run whose save a crash lost, maybe before it settled
        board.settle(owner, found[0], _gh(r).board)
    rec.board_number = found[0] if found else board.create(owner, title, _gh(r).board)
    return rec.board_number


def _link(r: Run, f: board.Fields, owner: str, repos: list[str]) -> str:
    """Link the board to the repositories of its owner; a note naming the
    others, which GitHub can't link (#230 D9)."""
    own = [x for x in repos if x.split("/")[0].lower() == owner.lower()]
    ids = [json.loads(r.sh(["gh", "api", f"repos/{x}"]))["node_id"] for x in own]
    board.link(f, ids, _gh(r).board)
    others = [x for x in repos if x not in own]
    return f"not linked, another owner: {', '.join(others)}" if others else ""


# -- config-pr ----------------------------------------------------------------

# Temporary: the record's layout is private to this module, but abandoning
# an onboarding takes deleting it by hand until `abandon` ships.
ABANDON = ("abandon the onboarding: delete its record in state_dir/onboarding/ and "
           "close its infra PR by hand, for now, until `agent-ops project abandon` ships")

NO_INFRA_TOKEN = blocked("GH_INFRA_TOKEN is unset: agent-ops-onboard loads it from "
                         "its op.env entry; run project add through that unit")


def _infra_env() -> dict | None:
    """The env of every agent-ops-infra call: GH_TOKEN is the infra token,
    which only the onboarding unit loads. None without it."""
    token = os.environ.get("GH_INFRA_TOKEN")
    return {**os.environ, "GH_TOKEN": token} if token else None


def _scratch(r: Run) -> Path:
    return Path(r.cfg.state_dir) / RECORDS_DIR / "infra"


def _git(r: Run, env: dict, *args: str) -> str:
    return r.sh([*GIT_AS_GH, "-C", str(_scratch(r)), *args], env=env)


def _infra_main(r: Run, env: dict) -> str:
    """infra main's targets.yaml, fetched into the scratch clone."""
    if not (_scratch(r) / ".git").exists():
        shutil.rmtree(_scratch(r), ignore_errors=True)
        r.sh([*GIT_AS_GH, "clone", f"https://github.com/{INFRA_REPO}.git",
              str(_scratch(r))], env=env)
    _git(r, env, "fetch", "origin", "--prune")
    return _git(r, env, "show", f"origin/main:{INFRA_TARGETS}")


def _main_entry(rec: Record, text: str) -> Outcome | None:
    """None when infra main's targets.yaml has no entry for the project;
    verified when its entry is the confirmed proposal; blocked otherwise.
    Empty commands may be left out of an entry."""
    entry = next((p for p in yaml.safe_load(text).get("projects") or []
                  if p.get("name") == rec.project), None)
    if entry is None:
        return None
    if _comparable(entry) != _comparable(_config_proposal(rec.proposal, rec.board_number)):
        return blocked(f"infra main has a different {rec.project} entry than the "
                       f"confirmed proposal: reconcile {INFRA_TARGETS} on infra main")
    return Outcome(StepState.VERIFIED, "already on infra main", rec.pr_url)


def _comparable(entry: dict) -> dict:
    return {**entry, "repositories": [{k: v for k, v in x.items() if v != ""}
                                      for x in entry.get("repositories") or []]}


class OpenPr(NamedTuple):
    number: int
    url: str
    head: str


def _open_pr(r: Run, project: str, env: dict) -> OpenPr | None:
    rows = json.loads(r.sh(["gh", "pr", "list", "--repo", INFRA_REPO, "--head",
                            f"onboard/{project}", "--state", "open",
                            "--json", "number,url,headRefOid"], env=env))
    return OpenPr(rows[0]["number"], rows[0]["url"], rows[0]["headRefOid"]) if rows else None


def _config_pr(r: Run, rec: Record) -> Outcome:
    if not (env := _infra_env()):
        return NO_INFRA_TOKEN
    main = _infra_main(r, env)
    if on_main := _main_entry(rec, main):
        return on_main
    pr = _open_pr(r, rec.project, env)
    if pr and _pr_current(r, rec, pr, env):
        return Outcome(StepState.VERIFIED, link=rec.pr_url)
    return _push_pr(r, rec, main, pr, env)


def _pr_current(r: Run, rec: Record, pr: OpenPr, env: dict) -> bool:
    """The open PR is the record's push of its revision onto today's main."""
    ip = rec.infra_pr
    return ip is not None and (pr.head, ip.revision, ip.base, ip.builder) == (
        ip.head, rec.revision, _git(r, env, "rev-parse", "origin/main").strip(),
        _builder(r))


def _push_pr(r: Run, rec: Record, main: str, pr: OpenPr | None, env: dict) -> Outcome:
    """Push the branch, then open its PR unless one is open."""
    pushed = _push(r, rec, main, env, pr.url if pr else rec.pr_url)
    if isinstance(pushed, Outcome):
        return pushed
    number, url = (pr.number, pr.url) if pr else _create_pr(r, rec, env)
    _record_push(r, rec, number, url, pushed)
    _resource(rec, "infra-pr", url)
    return Outcome(StepState.VERIFIED, link=url)


class Pushed(NamedTuple):
    """The branch's head after _push: pushed by this run's builder, or
    adopted, built by an unknown revision."""
    head: str
    base: str
    builder: str      # "" when adopted
    adopted: bool


def _record_push(r: Run, rec: Record, number: int, url: str, pushed: Pushed) -> None:
    """The PR now at `pushed`; its CI window starts now. An adoption is
    rebuilt by this revision on the next run, not this one."""
    rec.infra_pr = InfraPr(number, url, pushed.head, pushed.base, rec.revision,
                           r.now.isoformat(), pushed.builder)
    if pushed.adopted:
        r.adopted.add(rec.project)


def _push(r: Run, rec: Record, main: str, env: dict, link: str) -> Pushed | Outcome:
    """onboard/<project> rebuilt on infra main and pushed over the head the
    record names (a lease), never over anyone else's. A head the record
    doesn't name is adopted when it is this very commit, pushed before a
    crash lost the record's save (#210: a crash between create and record
    still finds the resource); otherwise it blocks, with `link`."""
    branch = f"onboard/{rec.project}"
    built = _commit(r, rec, main, env)
    if isinstance(built, Outcome):
        return built
    head, base, tree = built
    builder = _builder(r)   # unknown identity fails before anything is pushed or adopted
    ours = rec.infra_pr.head if rec.infra_pr else ""
    theirs = _git(r, env, "for-each-ref", "--format=%(objectname)",
                  f"refs/remotes/origin/{branch}").strip()
    if theirs != ours:
        if _git(r, env, "show", "-s", "--format=%P %T", theirs).split() == [base, tree]:
            return Pushed(theirs, base, "", adopted=True)
        return blocked(f"{branch} on {INFRA_REPO} is at {theirs}, which this onboarding "
                       f"didn't push (it pushed {ours or 'nothing yet'}): review it, then "
                       f"delete the branch, or {ABANDON}", link)
    _git(r, env, "push", *([f"--force-with-lease={branch}:{ours}"] if ours else []),
         "origin", f"HEAD:refs/heads/{branch}")
    return Pushed(head, base, builder, adopted=False)


def _commit(r: Run, rec: Record, main: str, env: dict) -> tuple[str, str, str] | Outcome:
    """The project added to infra main's targets.yaml, checked with the
    loader and committed on onboard/<project> in the scratch clone: its
    sha, parent and tree."""
    text = add_project_text(main, project_block(_config_proposal(rec.proposal, rec.board_number)))
    try:
        config.parse_config(yaml.safe_load(text))
    except ValueError as e:
        return blocked(f"infra's targets.yaml refuses the project: {e}")
    _git(r, env, "checkout", "-B", f"onboard/{rec.project}", "origin/main")
    (_scratch(r) / INFRA_TARGETS).write_text(text)
    _git(r, env, "add", INFRA_TARGETS)
    # The box's own git identity: its signing key is registered to it, so
    # GitHub shows the commit Verified.
    _git(r, env, "commit", "-m", f"feat(config): onboard {rec.project}")
    return (_git(r, env, "rev-parse", "HEAD").strip(),
            _git(r, env, "rev-parse", "origin/main").strip(),
            _git(r, env, "show", "-s", "--format=%T", "HEAD").strip())


def _create_pr(r: Run, rec: Record, env: dict) -> tuple[int, str]:
    url = r.sh(["gh", "pr", "create", "--repo", INFRA_REPO, "--base", "main",
                "--head", f"onboard/{rec.project}",
                "--title", f"feat(config): onboard {rec.project}",
                "--body", f"Adds project {rec.project}, paused, at proposal "
                          f"revision {rec.revision[:12]}. Opened by agent-ops-onboard: merge it "
                          "once its checks are green, and onboarding deploys it."],
               env=env).strip()
    return int(url.rsplit("/", 1)[1]), url


# -- config-merge -------------------------------------------------------------

CI_LIMIT = timedelta(minutes=30)


def _config_merge(r: Run, rec: Record) -> Outcome:
    """Verified once the operator merged the infra PR into main, by any merge
    method. Onboarding never merges it: a timer with merge rights on infra
    main, the box's deploy source, is too much authority. Until the merge
    the PR is rebuilt when main moves, and its checks are watched (#225:
    GitHub enforces nothing on agent-ops-infra); green blocks on the merge."""
    if not (env := _infra_env()):
        return NO_INFRA_TOKEN
    ip = rec.infra_pr
    if ip is None:
        return _added_on_main(r, rec, env)
    pr = json.loads(r.sh(["gh", "pr", "view", str(ip.number), "--repo", INFRA_REPO,
                          "--json", "state,baseRefName,mergeCommit,mergedAt"], env=env))
    if pr["state"] not in ("OPEN", "MERGED"):
        return blocked(f"the infra PR was closed: reopen it, or {ABANDON}", ip.url)
    merged = pr["state"] == "MERGED"
    if (base := pr["baseRefName"]) != "main":
        fix = f"nothing deploys a merge there; {ABANDON}" if merged else (
            "set its base back to main")
        return blocked(f"the infra PR's base is {base!r}, not main: {fix}", ip.url)
    if merged:
        return _merged(rec, pr["mergeCommit"]["oid"], pr["mergedAt"])
    return _rebuild_or_check(r, rec, env)


def _merged(rec: Record, sha: str, at: str) -> Outcome:
    rec.merge_sha, rec.merged_at = sha, datetime.fromisoformat(at).isoformat()
    return Outcome(StepState.VERIFIED, link=rec.pr_url)


def _added_on_main(r: Run, rec: Record, env: dict) -> Outcome:
    """config-pr found the project on infra main, so there is no PR: the
    merge is the latest main commit that added or removed its entry, in
    the scratch clone config-pr fetched. The entry there must still be the
    confirmed proposal."""
    on_main = _main_entry(rec, _git(r, env, "show", f"origin/main:{INFRA_TARGETS}"))
    if on_main is None or on_main.state is StepState.BLOCKED:
        return on_main or blocked(f"infra main no longer lists {rec.project}, and "
                                  f"there is no infra PR: {ABANDON}")
    found = _git(r, env, "log", "-1", "--format=%H %cI", "-S", f"- name: {rec.project}",
                 "origin/main", "--", INFRA_TARGETS).split()
    if not found:
        return blocked(f"infra main lists {rec.project}, but no commit there adds "
                       f"'- name: {rec.project}' to {INFRA_TARGETS}")
    return _merged(rec, *found)


def _rebuild_or_check(r: Run, rec: Record, env: dict) -> Outcome:
    """Rebuild the PR when infra main moved or another revision built it,
    else check it. A head adopted this run waits for the next run's rebuild."""
    ip = rec.infra_pr
    if rec.project in r.adopted:
        return _adopted(ip.url)
    main = _infra_main(r, env)
    moved = _git(r, env, "rev-parse", "origin/main").strip() != ip.base
    if not moved and ip.builder == _builder(r):
        return _checked(r, rec, env)
    pushed = _push(r, rec, main, env, ip.url)
    if isinstance(pushed, Outcome):
        return pushed
    _record_push(r, rec, ip.number, ip.url, pushed)
    if pushed.adopted:
        return _adopted(ip.url)
    if moved:
        return waiting("infra main moved: the PR was rebuilt on it", ip.url)
    return _checked(r, rec, env)


def _adopted(link: str) -> Outcome:
    return waiting("adopted the branch's head, pushed before a crash: this revision "
                   "rebuilds it next run", link)


def _checked(r: Run, rec: Record, env: dict) -> Outcome:
    """Green blocks on the operator's merge; pending waits, for 30 minutes."""
    ip = rec.infra_pr
    waiting_on = _ci_waiting_on(r, ip.head, env)
    if isinstance(waiting_on, Outcome):
        return waiting_on
    if not waiting_on:
        return blocked("infra PR is green: merge it, then rerun `agent-ops project "
                       "add` or let the timer carry on to deploy", ip.url)
    if r.now - datetime.fromisoformat(ip.pushed_at) > CI_LIMIT:
        return blocked(f"{waiting_on}, 30 minutes after the push", ip.url)
    return waiting(waiting_on, ip.url)


class Check(NamedTuple):
    name: str
    conclusion: str    # "" until it finishes
    link: str


def _ci_waiting_on(r: Run, head: str, env: dict) -> str | Outcome:
    """What `head` still waits for; "" once its ci run succeeded and every
    other workflow run and commit status finished green. A red one blocks,
    with its link."""
    runs = _workflow_runs(r, head, env)
    if not any(c.name == "ci" for c in runs):
        return "no ci run on the head yet"
    checks = [*runs, *_statuses(r, head, env)]
    if red := next((c for c in checks if ci_verdict(c.conclusion) == "red"), None):
        return blocked(f"infra check {red.name} is {red.conclusion}", red.link)
    pending = [c.name for c in checks if ci_verdict(c.conclusion) != "green"]
    return f"checks pending: {', '.join(pending)}" if pending else ""


def _api(r: Run, env: dict, path: str) -> dict:
    return json.loads(r.sh(["gh", "api", f"repos/{INFRA_REPO}/{path}"], env=env))


def _workflow_runs(r: Run, head: str, env: dict) -> list[Check]:
    """The latest run of each workflow on `head`. A fine-grained token cannot
    read check runs, and every infra check is an Actions job, so a run's
    conclusion stands for its jobs."""
    latest: dict[str, dict] = {}
    for x in sorted(_api(r, env, f"actions/runs?head_sha={head}&per_page=100")[
            "workflow_runs"], key=lambda x: x["id"]):
        latest[x["name"]] = x
    return [_run_check(x) for x in latest.values()]


def _run_check(x: dict) -> Check:
    """A finished run's conclusion; skipped and neutral are green for any
    workflow but ci, which must succeed."""
    done = x["conclusion"] if x["status"] == "completed" else ""
    strict = x["name"] == "ci" and done != "success" and ci_verdict(done) == "green"
    return Check(x["name"], "" if strict else done, x["html_url"])


def _statuses(r: Run, head: str, env: dict) -> list[Check]:
    return [Check(s["context"], "" if s["state"] == "pending" else s["state"],
                  s["target_url"] or "")
            for s in _api(r, env, f"commits/{head}/status")["statuses"]]


# -- deploy -------------------------------------------------------------------

DEPLOY_LIMIT = timedelta(minutes=15)
UPDATER = "agent-ops-update.service"


def _deploy(r: Run, rec: Record) -> Outcome:
    """agent-ops-update deploys the merge (#225): verified once the box's
    infra checkout holds it and the live config loads with the project
    paused."""
    if _box_has(r, rec.merge_sha) and _live_paused(r, rec.project):
        rec.deployed_at = r.now.isoformat()
        return VERIFIED
    merged_at = datetime.fromisoformat(rec.merged_at)
    if journal := _updater_failure(r, merged_at):
        return blocked(f"{UPDATER} failed after the merge:\n{journal}")
    if r.now - merged_at > DEPLOY_LIMIT:
        return blocked("nothing deployed 15 minutes after the merge: is "
                       "agent-ops-update.timer running? "
                       "(systemctl --user status agent-ops-update.timer)")
    return waiting(f"waiting for {UPDATER} to deploy the merge")


def _box_has(r: Run, sha: str) -> bool:
    try:
        r.sh(["git", "-C", os.path.expanduser(config.INFRA_CHECKOUT), "merge-base",
              "--is-ancestor", sha, "HEAD"])
    except subprocess.CalledProcessError:
        return False
    return True


def _live_paused(r: Run, project: str) -> bool:
    """Read now, not at the run's start: the updater may have just
    installed it. One the loader refuses fails the step."""
    return any(p.name == project and p.paused
               for p in config.load_config(r.config_path).projects)


def _updater_failure(r: Run, since: datetime) -> str:
    """The updater's last journal lines when its last run failed after
    `since`; "" otherwise."""
    out = r.sh(["systemctl", "--user", "show", UPDATER, "-p", "Result",
                "-p", "ExecMainExitTimestamp", "--timestamp=unix"])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    ended = props.get("ExecMainExitTimestamp", "").lstrip("@")
    if props.get("Result") == "success" or not ended.isdigit() or (
            datetime.fromtimestamp(int(ended), timezone.utc) <= since):
        return ""
    return r.sh(["journalctl", "--user", "-u", UPDATER, "-n", "15",
                 "--no-pager", "-o", "cat"]).strip()


# -- visible ------------------------------------------------------------------

VISIBLE_LIMIT = timedelta(minutes=15)
# The web console reads targets.yaml only at startup, so ask the running one.
LOGIN = "agent-ops-onboard"


def _visible(r: Run, rec: Record) -> Outcome:
    if _listed_paused(r, rec.project):
        return VERIFIED
    if r.now - datetime.fromisoformat(rec.deployed_at) > VISIBLE_LIMIT:
        return blocked("the console doesn't list the project 15 minutes after the "
                       "deploy: check agent-ops-web.service "
                       "(systemctl --user status agent-ops-web.service)")
    return waiting("waiting for the console to list the project")


def _listed_paused(r: Run, project: str) -> bool:
    try:
        body = r.get(f"http://127.0.0.1:{config.web_port()}/api/projects",
                     {config.LOGIN_HEADER: LOGIN})
    except OSError:  # refused while the updater restarts it
        return False
    return any(p["name"] == project and p["paused"]
               for p in json.loads(body)["projects"])


STEPS = ("resolve", "clone", "inspect", "confirm", "board", "config-pr", "config-merge", "deploy",
         "visible")
_STEPS = {"resolve": _resolve, "clone": _clone, "inspect": _inspect, "confirm": _confirm_step,
          "board": _board,
          "config-pr": _config_pr, "config-merge": _config_merge, "deploy": _deploy,
          "visible": _visible}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m dispatcher.onboard")
    parser.add_argument("--config", default=config.default_config_path())
    run(parser.parse_args(argv).config)


if __name__ == "__main__":
    main()
