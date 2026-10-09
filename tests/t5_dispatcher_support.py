"""Task-flow seam with real Sessions/listener and only external-effect doubles."""

from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from dispatcher.config import Config, Target
from dispatcher.herdr import Tab
from dispatcher.main import Deps, run_pass
from dispatcher.models import DEFAULT_POLICY, Entry, ModelPolicy, Track
from dispatcher.runtime_http import RuntimeClient
from dispatcher.sessions import Sessions
from dispatcher.state import Stage, TaskState, load, save
from dispatcher.usage import ProviderUsage, Window, WindowKind
from dispatcher import usage_providers

from t5_runtime_support import RuntimeCase

PRODUCT_ROOT = Path(__file__).resolve().parents[1]


class GitHubDouble:
    def __init__(self):
        self.effects = []

    def viewer_login(self):
        return "fixture"

    def rank_rows(self, target):
        return []

    def candidates(self, target):
        return []

    def issue_state(self, repo, number):
        return "OPEN"

    def issue_view(self, repo, number):
        return {"number": number, "title": "generic fixture task", "state": "OPEN", "labels": []}

    def set_status(self, target, issue, option_id):
        self.effects.append(("status", issue, option_id))

    def comment(self, target, issue, body):
        self.effects.append(("comment", issue, body))

    def release(self, target, issue, reason):
        self.effects.append(("release", issue, reason))

    def add_label(self, target, issue, label):
        self.effects.append(("label", issue, label))

    def set_boost(self, target, issue, value):
        self.effects.append(("boost", issue, value))

    def pr_number_for_branch(self, target, branch):
        return 123

    def pr_view(self, target, pr_number):
        return {"state": "OPEN", "headRefOid": "fixture-head", "headRefName": "fixture-branch",
                "url": "https://github.com/fixture/repo/pull/123"}

    def ci_statuses(self, target, head, branch):
        return []

    def run_status(self, target, run_id):
        return ""

    def claim(self, target, candidate):
        self.effects.append(("claim", candidate.number))

    def cancel(self, target, issue):
        self.effects.append(("cancel", issue))

    def delete_branch(self, target, branch):
        self.effects.append(("delete", branch))

    def append_blocked_by(self, target, issue, blocker):
        self.effects.append(("blocked", issue, blocker))


class NotifierDouble:
    def __init__(self):
        self.effects = []

    def send(self, template, **context):
        self.effects.append((template, context))
        return len(self.effects) + 100


class UsageDouble:
    name = "openai"

    def fetch(self, state_dir, *, now=time.time):
        return ProviderUsage(provider="openai", source="oauth", fetched_at=now(), windows=(
            Window(kind=WindowKind.SESSION, scope=None, used=0.01,
                   resets_at=datetime.now(timezone.utc) + timedelta(hours=5)),
            Window(kind=WindowKind.WEEKLY, scope=None, used=0.01,
                   resets_at=datetime.now(timezone.utc) + timedelta(days=7)),
        ))


class TabDouble:
    label = "fixture"
    workspace_id = "owned-workspace"
    tab_id = "owned-tab"
    pane_id = "owned-pane"

    def __init__(self, case):
        self.case = case
        self.alive = True
        self.effects = []

    def run(self, command):
        self.effects.append(("run", command))
        return True

    def read(self, source, lines):
        return "generic task terminal output"

    def agent_state(self):
        return ("waiting", 1)

    def send_text(self, text):
        self.effects.append(("input", text))
        return True

    def send_keys(self, *keys):
        self.effects.append(("keys", keys))
        return True

    def close(self):
        self.effects.append(("close", self.case.runtime.view()))
        self.alive = False
        return True


class DispatcherCase(AbstractContextManager):
    def __init__(self, monkeypatch, *, cap=10800, stage=Stage.REVIEW, now=20000):
        self.temporary = tempfile.TemporaryDirectory(prefix="t5d-", dir="/tmp")
        self.directory = Path(self.temporary.name)
        self.state_dir = self.directory / "state"
        self.worktree = self.directory / "worktree"
        self.state_dir.mkdir()
        self.worktree.mkdir()
        (self.worktree / ".agent").mkdir()
        self.log = (self.directory / "listener.log").open("w+")
        env = {"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin", "PYTHONPATH": str(PRODUCT_ROOT),
               "PYTHONPYCACHEPREFIX": str(self.directory / "bytecode"),
               "AGENT_OPS_STATE_DIR": str(self.state_dir), "HOME": str(self.directory / "home")}
        Path(env["HOME"]).mkdir()
        self.listener = subprocess.Popen([sys.executable, "-m", "dispatcher.waitd"], env=env, cwd=self.worktree,
                                         stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 5
        while not (self.state_dir / "wait" / "wait.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (self.state_dir / "wait" / "wait.sock").exists(), "real product listener fixture did not start"
        self.client = RuntimeClient(self.state_dir)
        bound_stage = "spec" if stage == Stage.AWAITING_SPEC_REVIEW else "review"
        self.runtime = RuntimeCase(self.state_dir, control=self.client, stage=bound_stage, worktree=str(self.worktree))
        self.tab = TabDouble(self)
        monkeypatch.setattr(Tab, "find", classmethod(lambda cls, label: self.tab))
        monkeypatch.setattr(Tab, "ensure", classmethod(lambda cls, workspace_label, label, cwd, env=None: self.tab))
        monkeypatch.setattr("telegram.inbound.fetch_events", lambda state_dir: [])
        monkeypatch.setitem(usage_providers.ADAPTERS, "openai", UsageDouble())
        original_run = subprocess.run
        self.process_effects = []
        def external_run(args, *positional, **kwargs):
            command = list(args) if not isinstance(args, str) else args.split()
            if command and Path(command[0]).name == "podman" and command[1:3] == ["rm", "-f"]:
                self.process_effects.append(("podman-end", self.runtime.view()))
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return original_run(args, *positional, **kwargs)
        monkeypatch.setattr(subprocess, "run", external_run)
        monkeypatch.setattr(time, "time", lambda: now)
        entry = Entry("openai", "fixture")
        track_name = DEFAULT_POLICY.untracked
        track = Track(track_name, "fixture", {name: (entry,) for name in ["spec", "plan", "implement", "review", "address-review"]})
        policy = ModelPolicy((entry,), track_name, {track_name: track})
        target = Target(name="fixture", repo="fixture/repo", clone_path=str(self.directory / "clone"),
                        worktrees_path=str(self.directory), rank_cmd="", project_number=1, project_owner="fixture",
                        status_field_id="status", status_ready_option_id="ready", status_in_progress_option_id="running",
                        models=policy)
        self.cfg = Config(state_dir=str(self.state_dir), capacity=1, session_memory="2g", session_cpus="1",
                          targets=[target], models=policy, background_wait_seconds=cap,
                          stall_after_seconds=600, spec_review_grace_minutes=0)
        self.task = TaskState(issue=501, target="fixture", stage=stage, slot=0, worktree=str(self.worktree),
                              branch="fixture-branch", title="generic fixture task", updated_at="1970-01-01T00:00:00+00:00",
                              track=track_name, picks={bound_stage: "openai/fixture"})
        save(self.state_dir, self.task)
        self.github = GitHubDouble()
        self.notifier = NotifierDouble()
        self.sessions = Sessions(state_dir=self.state_dir)
        self.deps = Deps(github=self.github, sessions=self.sessions, notifier=self.notifier)
        self.signal("working", stage=bound_stage)

    def signal(self, status, *, stage="review", **fields):
        (self.worktree / ".agent" / "stage.json").write_text(json.dumps({"stage": stage, "status": status, **fields}))

    def run(self):
        run_pass(self.cfg, self.deps)
        return load(self.state_dir, "fixture", 501)

    def physical_ends(self):
        return [snapshot for effect, snapshot in self.tab.effects if effect == "close"] + [snapshot for _, snapshot in self.process_effects]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.listener.terminate()
        try:
            self.listener.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.listener.kill()
            self.listener.wait(timeout=5)
        self.log.close()
        self.temporary.cleanup()
