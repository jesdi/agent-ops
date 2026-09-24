"""Acceptance tests for ticket 05: a stage's provider is fixed once it has a
pick. An override (web resume/run) or a resume intent (dispatcher) that names
a model of another provider than the pick must be rejected — same provider,
or no pick yet, still goes through. Black-box: HTTP status/body and dispatcher
observable state only, no internals.

Both `anthropic/claude-opus-5` and `openai/gpt-5-codex` are configured for
the `implement` stage below, so "not configured" is never the reason a
cross-provider override is rejected."""
from fastapi.testclient import TestClient

from dispatcher import eventlog, intents as intents_mod
from dispatcher.config import Config, Target
from dispatcher.models import parse_policy
from dispatcher.state import PARK_HUMAN, Stage, TaskState, save
import dispatcher.main as main
from tests.webfakes import HEADERS, FakeSources, make_task
from tests.usagefakes import session_usage
from web.app import create_app

MULTI_PROVIDER_POLICY = parse_policy({
    "triage": ["claude-opus-5"],
    "untracked": "standard",
    "tracks": {
        "standard": {
            "when": "Everything.",
            "spec": ["claude-opus-5"],
            "plan": ["claude-opus-5"],
            "implement": ["anthropic/claude-opus-5", "openai/gpt-5-codex"],
            "review": ["claude-opus-5"],
        },
    },
})


def _target(name="alpha"):
    return Target(
        name=name, repo=f"jesdi/{name}", clone_path=f"/tmp/clones/{name}",
        worktrees_path=f"/tmp/worktrees/{name}", rank_cmd="false",
        setup_cmd="", verify_cmd="", project_number=1, project_owner="jesdi",
        status_field_id="F", status_ready_option_id="R",
        status_in_progress_option_id="P", boost_field_id="B")


def _config(state_dir):
    return Config(state_dir=str(state_dir), capacity=2, session_memory="2g",
                 session_cpus="2", targets=[_target()],
                 models=MULTI_PROVIDER_POLICY)


def rig(tmp_path):
    fake = FakeSources()
    fake.tasks_list = [make_task(
        issue=7, picks={"implement": "anthropic/claude-opus-5"})]
    fake.usages = {"anthropic": session_usage(0.5), "openai": session_usage(0.5)}
    return fake, TestClient(create_app(_config(tmp_path), fake))


# --- criterion: cross-provider override rejected on both routes (422) -----

def test_resume_rejects_a_cross_provider_override_when_the_stage_has_a_pick(
        tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/alpha/7/resume", headers=HEADERS,
                    json={"model": "openai/gpt-5-codex"})
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "stage implement runs on anthropic; pick a anthropic model")
    assert fake.intents == []


def test_run_rejects_a_cross_provider_override_when_the_stage_has_a_pick(
        tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/alpha/7/run", headers=HEADERS,
                    json={"model": "openai/gpt-5-codex"})
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "stage implement runs on anthropic; pick a anthropic model")
    assert fake.execution_overrides == {}
    assert fake.intents == []


# --- criterion: same-provider override still accepted (green-guard) -------

def test_resume_accepts_a_same_provider_override(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/alpha/7/resume", headers=HEADERS,
                    json={"model": "anthropic/claude-opus-5"})
    assert r.status_code == 202
    assert fake.intents[-1][3] == {"model": "anthropic/claude-opus-5"}


# --- criterion: no pick yet accepts any configured model (green-guard) ----

def test_resume_with_no_pick_accepts_a_cross_provider_model(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, picks={})]
    r = client.post("/api/task/alpha/7/resume", headers=HEADERS,
                    json={"model": "openai/gpt-5-codex"})
    assert r.status_code == 202
    assert fake.intents[-1][3] == {"model": "openai/gpt-5-codex"}


# --- criterion: a parked pr-open task is checked against the implement pick

def test_resume_on_a_parked_pr_open_task_is_checked_against_the_implement_pick(
        tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(
        issue=7, stage=Stage.PR_OPEN, park=PARK_HUMAN,
        picks={"implement": "anthropic/claude-opus-5"})]
    r = client.post("/api/task/alpha/7/resume", headers=HEADERS,
                    json={"model": "openai/gpt-5-codex"})
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "stage implement runs on anthropic; pick a anthropic model")
    assert fake.intents == []


# --- criterion: a cross-provider resume intent that reaches the dispatcher
# (bypassing the web) is dropped with an event, and the task stays parked --

def test_dispatcher_drops_a_cross_provider_resume_intent_and_records_an_event(
        tmp_path):
    c = _config(tmp_path)
    wt = tmp_path / "worktrees" / "alpha" / "task-7"
    (wt / ".agent").mkdir(parents=True, exist_ok=True)
    task = TaskState(
        issue=7, target="alpha", stage=Stage.IMPLEMENT, slot=0,
        worktree=str(wt), branch="agent/task-7", title="t",
        updated_at="2026-07-25T00:00:00+00:00", track="standard",
        park=PARK_HUMAN, park_msg_id=55,
        picks={"implement": "anthropic/claude-opus-5"})
    save(c.state_dir, task)
    intents_mod.write_intent(c.state_dir, "resume", "alpha", 7,
                             {"model": "openai/gpt-5-codex"}, "operator", 1)

    # This is the dispatcher's authoritative resume-intent path, exercised
    # directly (not the full run_pass) so this stays a test of that one
    # decision and not of triage/claim/usage-fetch machinery unrelated to it.
    main._apply_intents(c, main.Deps(github=None, sessions=None, notifier=None))

    from dispatcher.state import load
    reloaded = load(c.state_dir, "alpha", 7)
    assert reloaded.park == PARK_HUMAN, "the task must stay parked"

    events = [e for e in eventlog.read_tail(c.state_dir) if e["issue"] == 7]
    assert len(events) == 1, f"expected one drop event, got {events}"
    assert "openai" in (events[0]["detail"] + events[0].get("model", "")).lower()
