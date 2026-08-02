"""Hand-written fakes and builders shared by the web/ test suite."""
from dispatcher.budget import UsageSnapshot
from dispatcher.config import Config, Target
from dispatcher.state import Stage, TaskState

HEADERS = {"Tailscale-User-Login": "jesdi@github"}


def make_target(name="alpha", repo="jesdi/alpha"):
    return Target(
        name=name, repo=repo, clone_path="/tmp/clones/" + name,
        worktrees_path="/tmp/worktrees/" + name, rank_cmd="false",
        setup_cmd="", verify_cmd="", project_number=1, project_owner="jesdi",
        status_field_id="F", status_ready_option_id="R",
        status_in_progress_option_id="P", boost_field_id="B")


def make_config(state_dir, targets=None, capacity=2):
    return Config(
        state_dir=str(state_dir), capacity=capacity, budget_threshold=0.8,
        racing_minutes=30, racing_threshold=0.95, session_memory="2g",
        session_cpus="2", targets=list(targets or [make_target()]))


def make_task(issue=7, **kw):
    defaults = dict(
        issue=issue, target="alpha", stage=Stage.IMPLEMENT, slot=0,
        worktree=f"/tmp/worktrees/alpha/{issue}", branch=f"task/{issue}",
        title=f"Task {issue}", updated_at="2026-07-25T10:00:00+00:00")
    defaults.update(kw)
    return TaskState(**defaults)


class FakeSources:
    """In-memory stand-in for web.sources.Sources — same method surface."""

    def __init__(self):
        self.tasks_list = []
        self.rank = {}            # target name -> (rows, as_of, stale)
        self.descriptions = {}    # (repo, number) -> dict
        self.snapshot = UsageSnapshot(0.5, 120.0, "oauth")
        self.quarantine = []
        self.fingerprints = []
        self.open_issues = {}     # (repo, number) -> bool | None
        self.events = []
        self.pane_tails = {}
        self.pane_histories = {}
        self.history_calls = []   # (issue, lines) recorded for clamp tests
        self.alive = set()
        self.attached = set()
        self.intents = []         # (action, issue, payload, actor)
        self.pending = []
        self.applied_plans = []   # (target_name, issue, plan)
        self.appended = []        # (event, target, issue, actor, detail)
        self.fingerprint = ('{"board": "a", "budget": "a", "failures": "a",'
                            ' "history": "0", "queue": "a"}')
        self.heartbeat = None     # dict | None returned by pass_heartbeat()
        self._claims_paused = False
        self._triage_running = False

    def tasks(self):
        return list(self.tasks_list)

    def rank_rows(self, target):
        return self.rank.get(target.name,
                             ([], "2026-07-25T00:00:00+00:00", False))

    def usage(self):
        return self.snapshot

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

    def pane_tail(self, issue):
        return self.pane_tails.get(issue, "")

    def pane_history(self, issue, lines=2000):
        self.history_calls.append((issue, lines))
        return self.pane_histories.get(issue, "")

    def session_alive(self, issue):
        return issue in self.alive

    def submit_intent(self, action, issue, payload, actor):
        self.intents.append((action, issue, payload, actor))
        return f"1753430000000-{issue}-{action}.json"

    def pending_intents(self):
        return list(self.pending)

    def apply_queue_plan(self, target, issue, plan):
        self.applied_plans.append((target.name, issue, plan))

    def append_event(self, event, *, target="", issue=0, actor="",
                     detail=""):
        self.appended.append((event, target, issue, actor, detail))

    def state_fingerprint(self):
        return self.fingerprint

    def mark_attached(self, issue):
        self.attached.add(issue)

    def clear_attached(self, issue):
        self.attached.discard(issue)

    def has_attached(self, issue):
        return issue in self.attached

    def issue_description(self, repo, number):
        return self.descriptions.get(
            (repo, number),
            {"title": "", "body": "", "url": "", "fetched_at": "",
             "error": "not seeded"})
