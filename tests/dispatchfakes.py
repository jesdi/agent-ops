"""Hand-written fakes and builders shared by the dispatcher test suite: the
GitHub, sessions and notifier fakes a pass runs over, a one-repository config,
saved-task builders and the patches that keep a pass off the network."""
import json
import subprocess
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dispatcher.main as main
import telegram.inbound as inbound
from dispatcher.usage import Verdict
from dispatcher import deps as deps_mod
from dispatcher import claiming, drive, executor, launch_choice, stage_launch
from dispatcher.deps import Deps
from dispatcher import github_app, host_auth
from dispatcher.config import Board, Config, Project, Repository
from dispatcher.models import parse_policy
from dispatcher.status import Kind
from dispatcher.state import (NO_SLOT, PlanApprovalRequest, SessionRecord, Stage, TaskState,
                              load, load_all, save, write_session)
from tests.statuses import PR, RUNNING, held
from tests.usagefakes import session_usage

# Every dispatcher module that reads the `datetime` name at runtime.
_CLOCK_READERS = (main, deps_mod, launch_choice, stage_launch, drive)


def _ago(**kw):
    """An ISO timestamp `kw` (timedelta arguments) before now."""
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def freeze_clock(monkeypatch, at):
    """`datetime.now()` returns `at` in every dispatcher module, whatever tz is asked."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    for module in _CLOCK_READERS:
        monkeypatch.setattr(module, "datetime", Frozen)


POLICY = parse_policy({
    "triage": ["claude-sonnet-5@low"],
    "untracked": "standard",
    "tracks": {
        "trivial": {"when": "Rote edits.",
                    "spec": ["claude-sonnet-5"], "plan": ["claude-sonnet-5"],
                    "implement": ["claude-sonnet-5"], "review": ["claude-sonnet-5"]},
        "standard": {"when": "Everything else.",
                     "spec": ["claude-opus-5"], "plan": ["claude-opus-5"],
                     "implement": ["claude-opus-5"], "review": ["claude-opus-5"]},
        "deep": {"when": "Big or risky.",
                 "spec": ["claude-fable-5-1@high", "claude-opus-5"],
                 "plan": ["claude-fable-5-1@high", "claude-opus-5"],
                 "implement": ["claude-opus-5@medium"],
                 "review": ["openai/gpt-sol@high", "claude-fable-5-1@high", "claude-opus-5"]},
    },
})
ADMIT_ALL = lambda m: Verdict(admitted=True, provider="anthropic", binding=None, reason="ok")  # noqa: E731
DENY_ALL = lambda m: Verdict(admitted=False, provider="anthropic", binding=None, reason="over-pace")  # noqa: E731


class FakeGitHub:
    auth = host_auth.STORED

    def __init__(self, cands=(), run_conclusion="", run_status_raises=False,
                 issue_states=None, issue_state_raises=False, rows=(),
                 cands_by_repository=None):
        self.cands = list(cands)
        # Per-repository override: real candidates() is project-scoped (each
        # project ranks its own board). Tests with one repository never set this and
        # candidates() falls back to self.cands as before.
        self.cands_by_repository = dict(cands_by_repository or {})
        self.claimed, self.released, self.canceled = [], [], []
        self.run_conclusion = run_conclusion  # "" = still running
        self.run_status_raises = run_status_raises
        self.created_issues = []              # (repo, title, body)
        self.next_issue = 500
        self.issue_states = issue_states or {}  # (repo, number) -> state
        self.issue_state_raises = issue_state_raises
        self.blocked_by = []                  # (issue, blocker)
        self.rows = list(rows)
        self.boosts, self.labeled, self.statused = [], [], []
        self.boost_raises = False
        self.lookup_raises = False
        self.pr_payloads = {}        # pr_number -> gh pr view payload
        self.pr_view_raises = False
        self.ci = []
        self.ci_calls = []
        self.branch_prs = {}         # branch -> pr number
        self.deleted_branches = []   # (repo, branch)
        self.login = "agent-bot"
        self.comments = []           # (issue, body)
        self.comment_raises = False

    def issue_body(self, repository, issue):
        return ""

    def rank_rows(self, repositories):
        if self.boost_raises:
            raise subprocess.CalledProcessError(1, ["rank"])
        return [{"repository": repositories[0].name, **r} for r in self.rows]

    def set_boost(self, repository, issue, value):
        if self.lookup_raises:
            raise LookupError(f"issue {issue} not found in project items")
        self.boosts.append((issue, value))

    def add_label(self, repository, issue, label):
        self.labeled.append((issue, label))

    def set_status(self, repository, issue, status):
        self.statused.append((issue, status))

    def candidates(self, repositories):
        """Real candidates() ranks once per project: this reads the list of
        the project's first repository, a Candidate naming no repository
        tagged with that one."""
        first = repositories[0].name
        return [c if c.repository else dc_replace(c, repository=first)
                for c in self.cands_by_repository.get(first, self.cands)]

    def claim(self, repository, cand):
        self.claimed.append(cand.number)

    def comment(self, repository, issue, body):
        if self.comment_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        self.comments.append((issue, body))

    def release(self, repository, issue, reason):
        self.released.append((issue, reason))

    def cancel(self, repository, issue):
        self.canceled.append(issue)

    def run_status(self, repository, run_id):
        if self.run_status_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        return self.run_conclusion

    def create_issue(self, repo, title, body):
        self.created_issues.append((repo, title, body))
        self.next_issue += 1
        return self.next_issue

    def issue_state(self, repo, number):
        if self.issue_state_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        return self.issue_states.get((repo, number), "OPEN")

    def close_issue(self, repo, number):
        self.issue_states[(repo, number)] = "CLOSED"

    def append_blocked_by(self, repository, issue, blocker):
        self.blocked_by.append((issue, blocker))

    def viewer_login(self):
        return self.login

    def pr_view(self, repository, pr_number):
        if self.pr_view_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        # Unknown PRs read as quiet/open, so tests that merely pass through
        # a pr-open task (e.g. the capture test) never crash the pass.
        return self.pr_payloads.get(pr_number, {
            "state": "OPEN", "mergedAt": None, "reviewDecision": "",
            "reviews": [], "comments": []})

    def ci_statuses(self, repository, head, branch):
        self.ci_calls.append((head, branch))
        return self.ci

    def pr_number_for_branch(self, repository, branch):
        return self.branch_prs.get(branch, 0)

    def delete_branch(self, repository, branch):
        self.deleted_branches.append((repository.repo, branch))


class FakeSessions:
    def __init__(self, alive=(), idle=None, tail="…pane tail…",
                 resume_raises=(), spawn_raises=()):
        self.alive_set = set(alive)
        self.idle = dict(idle or {})
        self.idle_queried = []
        self.tail = tail
        self.spawned, self.resumed, self.ended = [], [], []
        self.sent_text = []
        self.end_calls = []  # (repository, issue) — the repository end() actually got
        self.call_log = []  # ("capture_tail"|"end", repository, issue) — ordered
        # Parallel (repository, issue) records for spawn_stage/resume, so
        # multi-repository tests can assert which repository a spawn/resume was for
        # without disturbing the existing spawned/resumed tuple shapes.
        self.spawn_calls = []
        self.resume_calls = []
        self.gh_configs = []   # the gh config each spawn or resume mounts
        # Issues whose launch explodes the way a vanished worktree does:
        # the launcher checks <worktree>/.git before it builds the command to
        # mount, so a swept or half-removed checkout raises here.
        self.resume_raises = set(resume_raises)
        self.spawn_raises = set(spawn_raises)

    # Every method takes `repository` first, matching dispatcher.sessions.Sessions
    # post-rekey — but the fakes still record/key by issue alone, since every
    # test here (bar the repository-disambiguation ones) uses a single repository and
    # the recorded issue is what assertions check.
    def is_alive(self, repository, issue):
        return issue in self.alive_set

    def idle_seconds(self, repository, issue):
        self.idle_queried.append(issue)
        return self.idle.get(issue)

    def agent_state(self, repository, issue):
        return None  # herdr cannot be asked: a background wait holds

    def send_text(self, repository, issue, text):
        self.sent_text.append((issue, text))
        return True

    def spawn_stage(self, repository, issue, worktree, prompt, stage_name, model, effort="",
                    second=None, *, gh_config):
        if issue in self.spawn_raises:
            raise FileNotFoundError(
                f"[Errno 2] No such file or directory: '{worktree}/.git'")
        self.spawned.append((issue, stage_name, model, prompt, effort))
        self.spawn_calls.append((repository, issue))
        self.gh_configs.append(gh_config)

    def resume(self, repository, issue, worktree, message, model, effort="", second=None,
               *, session_id=None, gh_config):
        if issue in self.resume_raises:
            raise FileNotFoundError(
                f"[Errno 2] No such file or directory: '{worktree}/.git'")
        self.resumed.append((issue, message, model, effort))
        self.resume_calls.append((repository, issue))
        self.gh_configs.append(gh_config)

    def capture_tail(self, repository, issue, lines=25):
        self.call_log.append(("capture_tail", repository, issue))
        return self.tail

    def end(self, repository, issue):
        self.ended.append(issue)
        self.end_calls.append((repository, issue))
        self.call_log.append(("end", repository, issue))


class EveryRepoApp:
    """A registered GitHub App whose installation reaches every repository: the
    explicit App every dispatcher is wired with (Deps requires one)."""

    INSTALLATION = github_app.Installation(1, "https://github.com/settings/installations/1", "any")

    def registered(self):
        return True

    def installation(self, repo):
        return self.INSTALLATION

    def install_link(self, owner):
        return self.INSTALLATION.settings_url

    def mint(self, installation, repos, permissions):
        return github_app.Token("ghs_every_repo", datetime(2099, 1, 1, tzinfo=timezone.utc))


class FakeNotifier:
    def __init__(self, msg_id=77):
        self.msg_id = msg_id  # telegram/notify.Notifier returns 0 on send failure
        self.sent = []
        self.contexts = []
        self.calls = []  # (template, ctx) — parallel record; sent stays template-only
        # Repository of each send, taken from ctx["repository"] when the caller
        # passes one (None otherwise) — lets multi-repository tests assert which
        # project's notification fired without touching `sent`/`contexts`.
        self.repositories = []

    def send(self, template, **ctx):
        self.sent.append(template)
        self.contexts.append((template, ctx))
        self.calls.append((template, ctx))
        self.repositories.append(ctx.get("repository"))
        return self.msg_id


def cfg(tmp_path: Path) -> Config:
    return Config(
        state_dir=str(tmp_path / "state"), capacity=3,
        session_memory="2g", session_cpus="2",
        repositories=[Repository(
            name="portfolio_eval", repo="jesdi/portfolio_eval", github_id=1,
            clone_path=str(tmp_path / "repo"),
            worktrees_path=str(tmp_path / "repo.worktrees"), setup_cmd="setup",
            verify_cmd="make e2e-slot SLOT={slot}",
            gate_cmd="make gate-slot SLOT={slot}",
            project=Project(
                name="portfolio_eval",
                board=Board(number=1, owner="jesdi")))],
        infra_repo="jesdi/agent-ops",
        models=POLICY,
    )


def patch_usage(monkeypatch, util=0.2, **kw):
    monkeypatch.setattr(main, "fetch_all",
                        lambda cfg, **k: {"anthropic": session_usage(util, **kw)})


def patch_workspace(monkeypatch, tmp_path):
    def fake_create(repository, issue, base="main", dry_run=False, auth=None):
        wt = Path(repository.worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        return str(wt)

    monkeypatch.setattr(claiming, "create_workspace", fake_create)


def deps(gh=None, sess=None, notifier=None):
    return Deps(github=gh or FakeGitHub(),
                     sessions=sess or FakeSessions(),
                     notifier=notifier or FakeNotifier(), app=EveryRepoApp())


def make_task(c, issue=42, stage=Stage.IMPLEMENT, slot=0,
              updated_at="2026-07-21T00:00:00+00:00", track="standard",
              session=True, **kw):
    """A saved task. Its session has ended a turn, so its conversation is
    recorded and a continuation resumes it; `session=False` leaves no record.
    It runs, but at pr-open, where it is parked on its PR."""
    wt = Path(c.repositories[0].worktrees_path) / f"task-{issue}"
    (wt / ".agent").mkdir(parents=True, exist_ok=True)
    kw.setdefault("status", PR if stage is Stage.PR_OPEN else RUNNING)
    ts = TaskState(issue=issue, repository="portfolio_eval", stage=stage, slot=slot,
                   worktree=str(wt), branch=f"agent/task-{issue}", title="t",
                   updated_at=updated_at, track=track, **kw)
    save(c.state_dir, ts)
    if session:
        record_session(c, ts.stage.value, issue)
    return wt


def armed_request(task, artifact):
    """The gate's request armed for the plan revision on disk."""
    from dispatcher.artifacts import plan_revision
    rev = plan_revision(task.worktree, artifact, task.spec_path)
    return PlanApprovalRequest(rev.path, fingerprint=rev.fingerprint)


def approve_plan(c, wt, spec_path=""):
    """What the dispatcher records when it drains the console's approve: the
    plan revision now on disk, approved on the task of worktree `wt` (a stub
    review page when there is none). Nothing in the worktree approves."""
    from dispatcher.artifacts import REVIEW_PAGE, plan_revision
    if not (wt / REVIEW_PAGE).exists():
        (wt / REVIEW_PAGE).write_text(REVIEW_PAGE_STUB)
    rev = plan_revision(wt, REVIEW_PAGE, spec_path).fingerprint
    assert rev, "no plan revision to approve"
    [task] = [t for t in load_all(c.state_dir) if t.worktree == str(wt)]
    save(c.state_dir, dc_replace(task, plan_approval=rev))


def record_session(c, stage, issue=42):
    """What the stop hook does when the task's session ends a turn."""
    write_session(c.state_dir, "portfolio_eval", issue,
                  SessionRecord("recorded-session", stage))


def replace_capacity(c, n):
    return dc_replace(c, capacity=n)


SPEC_PATH = "specs/2026-10-12-x/spec.md"
PLAN_SUMMARY = ".agent/review.html"


def patch_events(monkeypatch, events):
    monkeypatch.setattr(inbound, "fetch_events", lambda sd: list(events))


def valid_spec(wt: Path) -> Path:
    """A spec draft that satisfies check_spec (title + 2 H2s + >=1500B)."""
    p = wt / "spec.md"
    p.write_text("# t — design\n\n## Problem\n\n" + "x " * 400
                 + "\n\n## Decisions\n\n" + "y " * 400)
    return p


LOGIN_TAIL = ("Browser didn't open? Use the url below to sign in:\n"
              "https://claude.ai/oauth/authorize?code=true&client_id=abc\n"
              "Paste code here if prompted >")


def gate_signal(wt: Path, artifact: str = ".agent/review.html") -> None:
    """The plan session's 'plan ready, review it' signal."""
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-review", "note": "plan ready",
         "artifact": artifact}))


def arm_gate(c, issue: int = 42, artifact: str = ".agent/review.html") -> None:
    """Bind the task's request to the plan that is on disk now, as the pass
    that armed it did: call it after the tickets and the summary are written."""
    t = load(c.state_dir, "portfolio_eval", issue)
    save(c.state_dir, dc_replace(t, status=held(armed_request(t, artifact))))


class LiveUntilEnded(FakeSessions):
    """A session is alive from spawn/resume until the dispatcher ends it —
    the property the gate-park path depends on (ending the session is what
    frees capacity), and the one a fixed `FakeSessions(alive=...)` set cannot
    express across a dozen passes."""

    def spawn_stage(self, repository, issue, worktree, prompt, stage_name, model, effort="",
                    second=None, *, gh_config):
        super().spawn_stage(repository, issue, worktree, prompt, stage_name, model, effort, gh_config=gh_config)
        self.alive_set.add(issue)

    def resume(self, repository, issue, worktree, message, model, effort="", second=None,
               *, session_id=None, gh_config):
        super().resume(repository, issue, worktree, message, model, effort,
                       session_id=session_id, gh_config=gh_config)
        self.alive_set.add(issue)

    def end(self, repository, issue):
        super().end(repository, issue)
        self.alive_set.discard(issue)


def payload(state="OPEN", merged_at=None, reviews=(), comments=(),
            mergeable="MERGEABLE", head="deadbeef"):
    return {"state": state, "mergedAt": merged_at, "reviewDecision": "",
            "reviews": list(reviews), "comments": list(comments),
            "mergeable": mergeable,
            "headRefOid": head, "headRefName": "agent/task-42"}


def patch_teardown(monkeypatch):
    """Records (wt, branch, dry_run) — dry_run included so the dry-run
    threading is asserted on what the callee actually received, not merely
    on the pass surviving."""
    removed = []
    monkeypatch.setattr(
        executor, "remove_workspace",
        lambda repository, wt, branch, dry_run=False: removed.append(
            (wt, branch, dry_run)))
    return removed


def pr_open_task(c, issue=42, pr_number=12, **kw):
    make_task(c, issue=issue, stage=Stage.PR_OPEN, slot=NO_SLOT,
              pr_number=pr_number, **kw)
    return load(c.state_dir, "portfolio_eval", issue)


GOOD_TICKET = ("# 01 — thing\n\n**What to build:** " + ("behaviour " * 30)
               + "\n\n**Blocked by:** None\n\n- [ ] It works end to end\n")
REVIEW_PAGE_STUB = '<meta name="agent-ops-review" content="1">'


def write_tickets(wt, n, page=True):
    """n valid tickets, and a review page (the gate wants both) unless one is there."""
    d = wt / ".agent" / "tickets"; d.mkdir(parents=True, exist_ok=True)
    if page and not (wt / ".agent" / "review.html").exists():
        (wt / ".agent" / "review.html").write_text(REVIEW_PAGE_STUB)
    for i in range(1, n + 1):
        (d / f"{i:02d}-t{i}.md").write_text(GOOD_TICKET)


SPEC_URL = ("https://github.com/jesdi/portfolio_eval/tree/agent/task-42/"
            "specs/2026-10-12-x")


def _plan_ready_task(c, **kw):
    """A plan session that reports its plan ready: valid tickets on disk and
    the review summary as the signal's artifact."""
    wt = make_task(c, stage=Stage.PLAN, spec_path=SPEC_PATH, **kw)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-review", "note": "plan ready",
         "artifact": PLAN_SUMMARY}))
    return wt


def _events(c, name):
    from dispatcher import eventlog
    return [e for e in eventlog.read_tail(c.state_dir) if e["event"] == name]


def parked(task):
    """No session runs for the task: it waits for the operator, a wake or an event."""
    return task.status.kind not in (Kind.RUNNING, Kind.HELD_PLAN_APPROVAL)


def feedback_pending(task):
    """Woken by its PR's attention, the address-review round not launched yet."""
    return task.status.kind is Kind.WAKING and task.status.cause.value.startswith("attention(")
