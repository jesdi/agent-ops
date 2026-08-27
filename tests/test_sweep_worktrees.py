"""E2E tests for provision/sweep-worktrees.sh with real git repos.

The script is a safety net for worktrees the dispatcher's own teardown
(workspace.remove_workspace) never got to — orphaned tasks whose state
file is gone, and tasks stuck at stage=failed whose PR actually merged.
Because it deletes, every refusal reason gets its own test: the gate is
the feature.

Rig shape: a bare `origin`, a clone with `main`, and per-task worktrees,
plus fake gh/tmux/podman on PATH. Git is real — merge/ancestry checks are
what we are testing, so stubbing git would test nothing.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "provision" / "sweep-worktrees.sh"

DAY = 86400


def _git(cwd, *args, **kw):
    env = {**os.environ, **kw.pop("env", {})}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True, env=env)


def _commit(cwd, msg, *, when=None):
    """Commit with a controllable date so staleness is testable."""
    env = {}
    if when is not None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.gmtime(time.time() - when * DAY))
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    _git(cwd, "commit", "-m", msg, "--no-gpg-sign", env=env)


class Rig:
    def __init__(self, tmp_path, bin_dir):
        self.tmp = tmp_path
        self.bin = bin_dir
        self.state = tmp_path / "state"
        self.state.mkdir(exist_ok=True)
        self.origin = tmp_path / "origin.git"
        self.clone = tmp_path / "clone"
        self.worktrees = self.clone / ".worktrees"
        self.repo = "fake/repo"
        self.closed_issues = set()
        self.sessions = tmp_path / "tmux-sessions"
        self.containers = tmp_path / "podman-names"
        self.sessions.write_text("")
        self.containers.write_text("")

    # --- fixture construction ------------------------------------------
    def build(self):
        _git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        _git(self.tmp, "clone", str(self.origin), str(self.clone))
        _git(self.clone, "config", "user.email", "t@example.com")
        _git(self.clone, "config", "user.name", "t")
        _git(self.clone, "config", "commit.gpgsign", "false")
        (self.clone / "README.md").write_text("base\n")
        (self.clone / ".my-skills.json").write_text('{"v": 1}\n')
        _git(self.clone, "add", "-A")
        _commit(self.clone, "init", when=90)
        _git(self.clone, "push", "-u", "origin", "main")
        self.worktrees.mkdir(parents=True, exist_ok=True)
        self.write_targets()
        return self

    def write_targets(self):
        (self.tmp / "targets.yaml").write_text(yaml.safe_dump({
            "state_dir": str(self.state),
            "targets": [{
                "name": "fake",
                "repo": self.repo,
                "clone_path": str(self.clone),
                "worktrees_path": str(self.worktrees),
            }],
        }))

    def task(self, n, *, merged=True, closed=True, dirty=False,
             skills_drift=False, unpushed=False, age_days=30,
             push=True, agent_dir=True, state_stage=None,
             task_json="new"):
        """Create worktree task-<n> on agent/task-<n> in whatever shape
        the test needs.

        task_json controls what .agent/task.json (written by
        workspace.create_workspace) looks like inside it:
          "new"    - {"issue": n, "target": "fake"} — every worktree
                     provisioned after the (target, issue) rekey (Task 3).
          "legacy" - {"issue": n} — provisioned before Task 3 added target.
          "missing"- no task.json at all — provisioned before task.json
                     itself existed.
        """
        br = f"agent/task-{n}"
        wt = self.worktrees / f"task-{n}"
        _git(self.clone, "worktree", "add", "-b", br, str(wt), "main")
        _git(wt, "config", "commit.gpgsign", "false")
        (wt / f"feature-{n}.txt").write_text(f"work for {n}\n")
        _git(wt, "add", "-A")
        _commit(wt, f"feat: task {n}", when=age_days)
        if push:
            _git(wt, "push", "-u", "origin", br)
        if merged:
            _git(self.clone, "merge", "--no-ff", "--no-gpg-sign",
                 "-m", f"merge {br}", br)
            _git(self.clone, "push", "origin", "main")
        if unpushed:
            (wt / f"extra-{n}.txt").write_text("later\n")
            _git(wt, "add", "-A")
            _commit(wt, f"feat: more for {n}", when=age_days)
        if dirty:
            (wt / "README.md").write_text("locally modified\n")
        if skills_drift:
            (wt / ".my-skills.json").write_text('{"v": 2}\n')
        if closed:
            self.closed_issues.add(n)
        if agent_dir:
            (wt / ".agent").mkdir(exist_ok=True)
            (wt / ".agent" / "stage.json").write_text(
                json.dumps({"stage": "implement", "status": "done"}))
            (wt / ".agent" / "plan.md").write_text("# plan\n")
            if task_json == "new":
                (wt / ".agent" / "task.json").write_text(
                    json.dumps({"issue": n, "target": "fake"}))
            elif task_json == "legacy":
                (wt / ".agent" / "task.json").write_text(
                    json.dumps({"issue": n}))
            elif task_json != "missing":
                raise ValueError(f"unknown task_json mode: {task_json!r}")
        if state_stage:
            (self.state / f"task-{n}.json").write_text(json.dumps({
                "issue": n, "target": "fake", "stage": state_stage,
                "slot": 0, "worktree": str(wt), "branch": br,
                "title": f"task {n}", "updated_at": "2026-07-01T00:00:00Z",
            }))
        self.age(wt, age_days)
        return wt

    def age(self, wt, days):
        when = time.time() - days * DAY
        os.utime(wt, (when, when))

    def live(self, *names):
        self.sessions.write_text("".join(f"{n}\n" for n in names))
        self.containers.write_text("".join(f"{n}\n" for n in names))

    def attach(self, n):
        (self.state / f"attached-{n}").write_text("")

    def attach_new(self, target, n):
        (self.state / f"attached-{target}-{n}").write_text("")

    # --- running -------------------------------------------------------
    def run(self, *args, expect=None):
        self._write_fakes()
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "AGENT_OPS_STATE_DIR": str(self.state),
            "AGENT_OPS_TARGETS": str(self.tmp / "targets.yaml"),
            "AGENT_OPS_FLOCK_WAIT": "5",
        }
        proc = subprocess.run(["bash", str(SCRIPT), *args], env=env,
                              capture_output=True, text=True)
        if expect is not None:
            assert proc.returncode == expect, (
                f"rc={proc.returncode}\nstdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}")
        return proc

    def _write_fakes(self):
        closed = " ".join(str(i) for i in sorted(self.closed_issues))
        gh = self.bin / "gh"
        gh.write_text(f"""#!/bin/sh
# gh issue view <n> --repo <r> --json state --jq .state
if [ "$1" = "issue" ] && [ "$2" = "view" ]; then
  for c in {closed}; do
    [ "$3" = "$c" ] && {{ echo CLOSED; exit 0; }}
  done
  echo OPEN
  exit 0
fi
exit 1
""")
        gh.chmod(0o755)
        tmux = self.bin / "tmux"
        tmux.write_text(f'#!/bin/sh\ncat "{self.sessions}"\n')
        tmux.chmod(0o755)
        podman = self.bin / "podman"
        podman.write_text(f'#!/bin/sh\ncat "{self.containers}"\n')
        podman.chmod(0o755)

    # --- assertions helpers --------------------------------------------
    def branch_exists(self, n):
        out = subprocess.run(["git", "branch", "--list", f"agent/task-{n}"],
                             cwd=str(self.clone), capture_output=True, text=True)
        return bool(out.stdout.strip())

    def remote_branch_exists(self, n):
        out = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"agent/task-{n}"],
            cwd=str(self.clone), capture_output=True, text=True)
        return bool(out.stdout.strip())


@pytest.fixture
def rig(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return Rig(tmp_path, bin_dir).build()


# --- the happy path ----------------------------------------------------

def test_removes_merged_closed_stale_worktree(rig):
    wt = rig.task(162)
    rig.run("--sweep", expect=0)
    assert not wt.exists()
    assert not rig.branch_exists(162)


def test_removal_deletes_the_remote_branch(rig):
    rig.task(162)
    assert rig.remote_branch_exists(162)
    rig.run("--sweep", expect=0)
    assert not rig.remote_branch_exists(162)


def test_removal_snapshots_the_agent_dir(rig):
    rig.task(162)
    rig.run("--sweep", expect=0)
    snap = rig.state / "autopsy" / "task-162-agent"
    assert (snap / "stage.json").exists()
    assert (snap / "plan.md").read_text() == "# plan\n"


def test_removal_drops_the_orphaned_state_file(rig):
    rig.task(162, state_stage="failed")
    rig.run("--sweep", expect=0)
    assert not (rig.state / "task-162.json").exists()


def test_removal_appends_an_event(rig):
    rig.task(162)
    rig.run("--sweep", expect=0)
    events = [json.loads(ln) for ln in
              (rig.state / "events.jsonl").read_text().splitlines() if ln]
    swept = [e for e in events if e["event"] == "worktree_swept"]
    assert len(swept) == 1
    assert swept[0]["issue"] == 162
    assert swept[0]["actor"] == "sweeper"


def test_failed_state_task_is_removed_when_merged(rig):
    """The 183/148 class: dispatcher says failed, git says merged. Git wins."""
    wt = rig.task(183, state_stage="failed")
    rig.run("--sweep", expect=0)
    assert not wt.exists()


# --- the refusal gate --------------------------------------------------

def test_refuses_unmerged_commits(rig):
    wt = rig.task(118, merged=False)
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert rig.branch_exists(118)
    assert "not in origin/main" in proc.stdout


def test_refuses_uncommitted_changes(rig):
    wt = rig.task(162, dirty=True)
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "uncommitted" in proc.stdout


def test_ignores_my_skills_json_provisioning_drift(rig):
    """.my-skills.json is modified in every worktree on the box; it is
    provisioning drift, never work, and must not block a sweep."""
    wt = rig.task(162, skills_drift=True)
    rig.run("--sweep", expect=0)
    assert not wt.exists()


def test_refuses_unpushed_commits(rig):
    wt = rig.task(199, unpushed=True)
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "unpushed" in proc.stdout


def test_refuses_open_issue(rig):
    wt = rig.task(162, closed=False)
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "issue" in proc.stdout


def test_refuses_live_tmux_session(rig):
    wt = rig.task(162)
    rig.live("task-162")
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "live" in proc.stdout


def test_refuses_when_operator_attached(rig):
    wt = rig.task(162)
    rig.attach(162)
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "attached" in proc.stdout


# --- (target, issue) rekey awareness ------------------------------------
# Post-rekey, live tmux sessions and podman containers are named
# task-<target>-<issue>, not the legacy task-<issue>; a sweeper that only
# ever checked the legacy name would false-negative its liveness guard for
# every task created after this deploy and rm -rf a live worktree.

def test_refuses_new_style_live_tmux_session(rig):
    wt = rig.task(162)  # task_json="new" by default -> target "fake"
    rig.live("task-fake-162")
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "live" in proc.stdout


def test_refuses_new_style_live_podman_container(rig):
    wt = rig.task(162)
    rig.containers.write_text("task-fake-162\n")
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "live" in proc.stdout


def test_still_refuses_legacy_live_session_name(rig):
    """A session not yet touched since the deploy (no adoption rename yet)
    is still named the old way — must still be caught."""
    wt = rig.task(162)
    rig.live("task-162")
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "live" in proc.stdout


def test_refuses_live_session_via_anchored_fallback_when_target_unknown(rig):
    """A worktree provisioned before Task 3 has no target in task.json — the
    sweeper cannot build the exact task-<target>-<issue> name, so it must
    fall back to an anchored task-<anything>-<issue> match. The anchor must
    not fire on an unrelated issue whose number merely ends the same
    (task-foo-1162 must never stand in for issue 162)."""
    wt = rig.task(162, task_json="legacy")
    rig.live("task-fake-162", "task-foo-1162")
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "live" in proc.stdout


def test_missing_task_json_also_uses_the_anchored_fallback(rig):
    wt = rig.task(162, task_json="missing")
    rig.live("task-fake-162")
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "live" in proc.stdout


def test_anchored_fallback_does_not_false_positive_on_a_different_issue(rig):
    """The mirror of the anchoring test above: with no OTHER live session
    around, a worktree whose target is unknown must still sweep cleanly."""
    wt = rig.task(162, task_json="legacy")
    rig.live("task-fake-1162")  # a different issue number, not 162
    rig.run("--sweep", expect=0)
    assert not wt.exists()


def test_new_style_attached_marker_refuses_removal(rig):
    wt = rig.task(200)
    rig.attach_new("fake", 200)
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "attached" in proc.stdout


def test_removal_drops_both_legacy_and_new_style_state_files(rig):
    wt = rig.task(162, state_stage="failed")
    (rig.state / "task-fake-162.json").write_text(json.dumps({
        "issue": 162, "target": "fake", "stage": "failed", "slot": -1,
        "worktree": str(wt), "branch": "agent/task-162", "title": "t",
        "updated_at": "2026-07-01T00:00:00Z",
    }))
    rig.run("--sweep", expect=0)
    assert not (rig.state / "task-162.json").exists()
    assert not (rig.state / "task-fake-162.json").exists()


def test_removal_drops_new_style_state_file_via_anchored_glob_when_target_unknown(rig):
    wt = rig.task(162, task_json="legacy")
    (rig.state / "task-fake-162.json").write_text("{}")
    (rig.state / "task-fake-1162.json").write_text("{}")  # distractor issue
    rig.run("--sweep", expect=0)
    assert not wt.exists()
    assert not (rig.state / "task-fake-162.json").exists()
    assert (rig.state / "task-fake-1162.json").exists()


def test_sweep_refuses_worktree_newer_than_the_threshold(rig):
    wt = rig.task(246, age_days=2)
    proc = rig.run("--sweep", expect=0)
    assert wt.exists()
    assert "stale" in proc.stdout


def test_stale_threshold_is_configurable(rig, monkeypatch):
    rig.task(246, age_days=3)
    monkeypatch.setenv("AGENT_OPS_WORKTREE_STALE_DAYS", "1")
    rig.run("--sweep", expect=0)
    assert not (rig.worktrees / "task-246").exists()


# --- modes -------------------------------------------------------------

def test_single_target_mode_ignores_staleness(rig):
    """`is it safe to remove this one` is a different question from
    `has it been abandoned` — an explicit target skips the age gate."""
    wt = rig.task(246, age_days=0)
    rig.run("246", expect=0)
    assert not wt.exists()


def test_single_target_mode_refuses_unsafe_with_distinct_exit_code(rig):
    wt = rig.task(118, merged=False)
    proc = rig.run("118", expect=3)
    assert wt.exists()
    assert "not in origin/main" in proc.stdout


def test_single_target_accepts_a_path(rig):
    wt = rig.task(162)
    rig.run(str(wt), expect=0)
    assert not wt.exists()


def test_single_target_rejects_unknown_task(rig):
    rig.run("999", expect=1)


def test_dry_run_changes_nothing(rig):
    wt = rig.task(162)
    proc = rig.run("--sweep", "--dry-run", expect=0)
    assert wt.exists()
    assert rig.branch_exists(162)
    assert rig.remote_branch_exists(162)
    assert "task-162" in proc.stdout


def test_sweep_removes_only_the_eligible_ones(rig):
    keep_open = rig.task(118, closed=False)
    keep_unmerged = rig.task(149, merged=False)
    keep_fresh = rig.task(246, age_days=1)
    go1 = rig.task(162)
    go2 = rig.task(200)
    rig.run("--sweep", expect=0)
    assert keep_open.exists() and keep_unmerged.exists() and keep_fresh.exists()
    assert not go1.exists() and not go2.exists()


# --- concurrency -------------------------------------------------------

def test_refuses_to_run_while_the_convergence_lock_is_held(rig):
    """A dispatcher pass mid-`git worktree add` must never race a sweep."""
    wt = rig.task(162)
    holder = subprocess.Popen(
        ["python3", "-c",
         "import fcntl,sys,time;f=open(sys.argv[1],'w');"
         "fcntl.flock(f,fcntl.LOCK_EX);time.sleep(30)",
         str(rig.state / "convergence.lock")])
    try:
        time.sleep(1)
        proc = rig.run("--sweep", expect=1)
        assert wt.exists()
        assert "lock" in (proc.stdout + proc.stderr)
    finally:
        holder.kill()
        holder.wait()
