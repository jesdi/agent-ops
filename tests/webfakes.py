"""Hand-written fakes and builders shared by the web/ test suite."""
from dispatcher.config import Config, Repository
from dispatcher import execution_overrides, state
from dispatcher.models import parse_policy
from tests.configfakes import make_project, with_board
from tests.usagefakes import session_usage
from dispatcher.state import Stage, TaskState
from web.sources import Sources, ArtifactListing

HEADERS = {"Tailscale-User-Login": "jesdi@github"}


def make_repository(name="alpha", repo="jesdi/alpha"):
    return with_board(Repository(
        name=name, repo=repo, github_id=1, clone_path="/tmp/clones/" + name,
        worktrees_path="/tmp/worktrees/" + name, project=make_project(name)),
        status_in_progress_option_id="P", boost_field_id="B")


def tracks_policy(**stage_lists):
    """A one-track policy 'standard' whose stage lists are the kwargs
    (default every stage = [claude-opus-5])."""
    stages = {s: stage_lists.get(s, ["claude-opus-5"])
              for s in ("spec", "plan", "implement", "review")}
    return parse_policy({"triage": ["claude-opus-5"], "untracked": "standard",
                         "tracks": {"standard": {"when": "Everything.", **stages}}})


def make_config(state_dir, repositories=None, capacity=2):
    return Config(
        state_dir=str(state_dir), capacity=capacity, session_memory="2g",
        session_cpus="2", repositories=list(repositories or [make_repository()]),
        models=tracks_policy())


def make_task(issue=7, **kw):
    defaults = dict(
        issue=issue, repository="alpha", stage=Stage.IMPLEMENT, slot=0,
        worktree=f"/tmp/worktrees/alpha/{issue}", branch=f"task/{issue}",
        title=f"Task {issue}", updated_at="2026-07-25T10:00:00+00:00",
        track="standard")
    defaults.update(kw)
    return TaskState(**defaults)


class FakeSources:
    """In-memory stand-in for web.sources.Sources — same method surface."""

    def __init__(self, state_dir=None):
        self.state_dir = state_dir
        self.tasks_list = []
        self.rank = {}            # repository name -> (rows, as_of, stale)
        self.descriptions = {}    # (repo, number) -> dict
        self.usages = {"anthropic": session_usage(0.5)}
        self.quarantine = []
        self.fingerprints = []
        self.open_issues = {}     # (repo, number) -> bool | None
        self.events = []
        self.pane_tails = {}
        self.pane_histories = {}
        self.history_calls = []   # (issue, lines) recorded for clamp tests
        self.alive = set()
        self.intents = []         # (action, issue, payload, actor)
        self.pending = []
        self.applied_plans = []   # (repository_name, issue, plan)
        self.appended = []        # (event, repository, issue, actor, detail)
        self.fingerprint = ('{"board": "a", "failures": "a",'
                            ' "history": "0", "queue": "a", "usage": "a"}')
        self.heartbeat = None     # dict | None returned by pass_heartbeat()
        self._claims_paused = False
        self._triage_running = False
        self.messages_by_key = {}     # (repository, issue) -> list[msgq.Message]
        self.undelivered = {}         # (repository, issue) -> int
        self.blocked_wakes = set()    # (repository, issue) with a wake-blocked marker
        self.execution_overrides = {}  # (repository, issue) -> model/bypass tuple

    def tasks(self):
        return list(self.tasks_list)

    def task(self, repository, issue):
        active = next((t for t in self.tasks_list if (t.repository, t.issue) == (repository, issue)), None)
        if active is not None or self.state_dir is None:
            return active
        return state.load_archived(self.state_dir, repository, issue)

    def artifacts(self, repository, issue):
        if self.state_dir is None:
            return ArtifactListing([], "", False)
        return Sources.artifacts(self, repository, issue)

    def _artifact_status(self, repository, issue, item, expired):
        return Sources._artifact_status(self, repository, issue, item, expired)

    def artifact_content(self, repository, issue, artifact_id):
        return Sources.artifact_content(self, repository, issue, artifact_id)

    def rank_rows(self, repository):
        return self.rank.get(repository.name,
                             ([], "2026-07-25T00:00:00+00:00", False))

    def usage(self, routed=()):
        return self.usages

    def execution_override(self, repository, issue):
        stored = self.execution_overrides.get((repository, issue))
        return stored and execution_overrides.ExecutionOverride(*stored)

    def set_execution_override(self, repository, issue, *, model, bypass_usage):
        self.execution_overrides[(repository, issue)] = (model, bypass_usage)

    def quarantine_entries(self):
        return list(self.quarantine)

    def fingerprint_entries(self):
        return list(self.fingerprints)

    def issue_open(self, repo, number):
        return self.open_issues.get((repo, number))

    def pass_heartbeat(self):
        return self.heartbeat

    def triage_state(self):
        return self._claims_paused, self._triage_running

    def events_tail(self, limit):
        return self.events[-limit:]

    def last_claims(self):
        return {}

    def stage_signal(self, worktree):
        return None

    def pane_tail(self, repository, issue):
        return self.pane_tails.get(issue, "")

    def pane_history(self, repository, issue, lines=2000):
        self.history_calls.append((issue, lines))
        return self.pane_histories.get(issue, "")

    def session_alive(self, repository, issue):
        return issue in self.alive

    def submit_intent(self, action, repository, issue, payload, actor):
        self.intents.append((action, repository, issue, payload, actor))
        return f"1753430000000-{repository}-{issue}-{action}.json"

    def pending_intents(self):
        return list(self.pending)

    def apply_queue_plan(self, repository, issue, plan):
        self.applied_plans.append((repository.name, issue, plan))

    def append_event(self, event, *, repository="", issue=0, actor="",
                     detail=""):
        self.appended.append((event, repository, issue, actor, detail))

    def state_fingerprint(self):
        return self.fingerprint

    def issue_description(self, repo, number):
        return self.descriptions.get(
            (repo, number),
            {"title": "", "body": "", "url": "", "fetched_at": "",
             "error": "not seeded"})

    def messages(self, repository, issue):
        return self.messages_by_key.get((repository, issue), [])

    def undelivered_counts(self):
        return dict(self.undelivered)

    def wake_blocked_issues(self):
        return set(self.blocked_wakes)
