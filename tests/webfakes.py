"""Hand-written fakes and builders shared by the web/ test suite."""
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
