"""Which entry a stage launches on: model picks, tracks, operator overrides and the usage gate."""
import json
from dataclasses import replace as dc_replace
from pathlib import Path

import dispatcher.main as main
from dispatcher import claiming, drive, launch_choice
from dispatcher import execution_overrides
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import (Stage, TaskState, load, save, SessionRecord, write_session)
from tests.dispatchfakes import (ADMIT_ALL, DENY_ALL, FakeGitHub, FakeSessions,
                                 approve_plan, cfg, deps, make_task, patch_usage,
                                 patch_workspace, replace_capacity, valid_spec, write_tickets)
from tests.statuses import human, held, waking
from dispatcher.status import Kind


def test_budget_denied_blocks_spawns_and_pings_once(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "A", "u")])
    d = deps(gh, FakeSessions())
    c = cfg(tmp_path)
    main.run_pass(c, d)
    main.run_pass(c, d)  # second pass must not re-ping
    assert gh.claimed == []
    assert d.notifier.sent.count("budget_stall") == 1


def test_forced_queue_candidate_claims_despite_usage_gate(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(
            model="claude-sonnet-4-6", bypass_usage=True))
    gh = FakeGitHub([Candidate(42, "Forced", "u42")])
    sess = FakeSessions()

    claiming._claim_new(c, deps(gh, sess), [c.repositories[0]], DENY_ALL, tuple, False)

    assert gh.claimed == [42]
    assert [spawn[:3] for spawn in sess.spawned] == [
        (42, "spec", "anthropic/claude-sonnet-4-6")]
    assert execution_overrides.load(
        c.state_dir, "portfolio_eval", 42) is None


def test_override_model_without_bypass_still_checked_by_usage_gate(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT)
    task = load(c.state_dir, "portfolio_eval", 42)
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(
            model="claude-sonnet-4-6", bypass_usage=False))

    launch, bypass, _ = launch_choice._choose_launch(
        c, c.repositories[0], task, Stage.IMPLEMENT, DENY_ALL, tuple,
        launch_choice._execution_override_for(c, task, Stage.IMPLEMENT))

    assert (launch, bypass) == (None, False)


def test_forced_queue_candidate_keeps_choice_when_no_slot(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 0)
    choice = execution_overrides.ExecutionOverride(
        model="claude-sonnet-4-6", bypass_usage=True)
    execution_overrides.save(c.state_dir, "portfolio_eval", 42, choice)

    claiming._claim_new(c, deps(FakeGitHub([Candidate(42, "Forced", "u42")])),
                    [c.repositories[0]], DENY_ALL, tuple, False)

    assert execution_overrides.load(
        c.state_dir, "portfolio_eval", 42) == choice


def test_budget_resume_pings_once(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    patch_workspace(monkeypatch, tmp_path)
    d = deps()
    patch_usage(monkeypatch, util=0.95)
    main.run_pass(c, d)
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(c, d)
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_resume") == 1


def test_spawn_uses_the_untracked_track_when_no_track_label(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=1, labels=("auto",))])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "anthropic/claude-opus-5")]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.track == "standard"
    assert t.picks == {"spec": "anthropic/claude-opus-5"}


def test_spawn_uses_the_track_label_for_spec(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=None, labels=("auto", "track:deep"))])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))
    assert [s[:3] + (s[4],) for s in sess.spawned] == [
        (42, "spec", "anthropic/claude-fable-5-1", "high")]
    assert load(c.state_dir, "portfolio_eval", 42).track == "deep"


def test_spec_resolution_skips_a_denied_entry_to_the_next(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.2, fable=0.9)      # Fable week over pace
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", labels=("auto", "track:deep"))])
    sess = FakeSessions()
    main.run_pass(cfg(tmp_path), deps(gh, sess))
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "anthropic/claude-opus-5")]


def test_nothing_admitted_waits_without_claiming(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", labels=("auto",))])
    sess = FakeSessions()
    main.run_pass(cfg(tmp_path), deps(gh, sess))
    assert sess.spawned == [] and gh.claimed == []


def test_spec_done_signal_sets_the_task_track_and_spawns_plan_on_it(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard")
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": "spec.md", "track": "trivial"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert [s[:3] for s in sess.spawned] == [(42, "plan", "anthropic/claude-sonnet-5")]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.track == "trivial" and t.picks["plan"] == "anthropic/claude-sonnet-5"


def test_pre_router_task_runs_on_the_untracked_track(tmp_path, monkeypatch):
    """A task claimed before tracks existed has no track, and its spec signal
    carries none: it adopts the untracked track instead of parking or being
    bounced back to spec, and keeps it."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="")
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": "spec.md"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert [s[:3] for s in sess.spawned] == [(42, "plan", "anthropic/claude-opus-5")]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.track == "standard" and t.status.kind is Kind.RUNNING


def test_forced_pick_is_used_for_the_implement_session(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN, gated=True, track="deep",
                   picks={"implement": "anthropic/claude-sonnet-5@low"})  # a forced pick
    write_tickets(wt, 2)
    approve_plan(c, wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps({"stage": "plan", "status": "done"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert [s[:3] + (s[4],) for s in sess.spawned] == [
        (42, "implement", "anthropic/claude-sonnet-5", "low")]


def test_denied_pick_waits_and_never_re_walks_the_list(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.2, fable=0.9)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="deep",
                   picks={"plan": "anthropic/claude-fable-5-1@high"})
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": "spec.md", "track": "deep"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert sess.spawned == []
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.SPEC


def test_review_prefers_a_provider_other_than_implements(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.IMPLEMENT, track="deep",
                   ticket_count=1,
                   picks={"implement": "anthropic/claude-opus-5@medium"})
    (wt / ".agent" / "stage.json").write_text(json.dumps({"stage": "implement", "status": "done"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    # openai/gpt-sol has no adapter: unrated, so it ranks last, and denied.
    # The first admitted anthropic entry wins.
    assert [s[:3] for s in sess.spawned] == [(42, "review", "anthropic/claude-fable-5-1")]


def test_unknown_task_track_parks_with_a_note(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.IMPLEMENT, track="renamed")
    (wt / ".agent" / "stage.json").write_text(json.dumps({"stage": "implement", "status": "working"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN and "'renamed'" in t.status.note


def test_spawn_writes_effort_into_stage_json_and_models_log(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", labels=("auto", "track:deep"))])
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, FakeSessions()))
    agent_dir = Path(c.repositories[0].worktrees_path) / "task-42" / ".agent"
    sig = json.loads((agent_dir / "stage.json").read_text())
    assert (sig["model"], sig["effort"]) == ("anthropic/claude-fable-5-1", "high")
    assert (agent_dir / "models.log").read_text().strip().endswith(
        "spec anthropic/claude-fable-5-1@high")


def test_budget_edge_keys_on_the_untracked_spec_entry(tmp_path, monkeypatch):
    """Fable over pace must not stall the box when the untracked track specs
    on Opus."""
    patch_usage(monkeypatch, util=0.2, fable=0.9)
    patch_workspace(monkeypatch, tmp_path)
    d = deps()
    main.run_pass(cfg(tmp_path), d)
    assert d.notifier.sent.count("budget_stall") == 0


def test_drive_task_admits_per_spawned_stage_model(tmp_path, monkeypatch):
    """Stage model routing in _drive_task: SpawnStage uses the repository stage's
    model, not a single global flag. Fable over pace on task A's PLAN just
    skips to the next entry in the deep track's plan list (Opus) rather than
    blocking — resolution walks the list, it doesn't stop at the first deny.
    Task B's REVIEW proceeds independently on its own (trivial) track."""
    patch_usage(monkeypatch, util=0.2, fable=0.9)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    # Task A: SPEC + done signal → SpawnStage(PLAN); deep's
    # plan list is [Fable@high, Opus] — Fable over pace, so Opus wins.
    wt_a = Path(c.repositories[0].worktrees_path) / "task-1"
    (wt_a / ".agent").mkdir(parents=True)
    spec = wt_a / "spec.md"
    spec.write_text("# t — design\n\n## Problem\n\n" + "x " * 400
                    + "\n\n## Decisions\n\n" + "y " * 400)
    (wt_a / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md",
         "track": "deep"}))
    save(c.state_dir, TaskState(issue=1, repository="portfolio_eval",
                                stage=Stage.SPEC, slot=0,
                                worktree=str(wt_a), branch="agent/task-1",
                                title="A", track="deep",
                                updated_at="2026-07-14T00:00:00+00:00"))
    # Task B: IMPLEMENT done → SpawnStage(REVIEW) → trivial → Sonnet
    wt_b = Path(c.repositories[0].worktrees_path) / "task-2"
    (wt_b / ".agent").mkdir(parents=True)
    (wt_b / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "done", "note": ""}))
    save(c.state_dir, TaskState(issue=2, repository="portfolio_eval",
                                stage=Stage.IMPLEMENT, slot=1,
                                ticket_count=1,
                                worktree=str(wt_b), branch="agent/task-2",
                                title="B", track="trivial",
                                updated_at="2026-07-14T00:00:00+00:00"))
    sess = FakeSessions(alive={1, 2})
    main.run_pass(c, deps(sess=sess))
    spawned_issues = [s[0] for s in sess.spawned]
    assert 1 in spawned_issues, "Fable-over-pace must skip to Opus, not block PLAN"
    assert [s[:3] for s in sess.spawned if s[0] == 1] == [
        (1, "plan", "anthropic/claude-opus-5")]
    assert 2 in spawned_issues, "trivial-track REVIEW spawn must proceed"
    assert load(c.state_dir, "portfolio_eval", 1).stage is Stage.PLAN
    assert load(c.state_dir, "portfolio_eval", 2).stage is Stage.REVIEW


def test_budget_stall_note_names_the_binding_window(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    d = deps()
    main.run_pass(cfg(tmp_path), d)
    (note,) = [k["note"] for t, k in d.notifier.calls if t == "budget_stall"]
    assert note.startswith("anthropic session: 95% used, allowance 80%, headroom −15 pts")


def test_resume_ping_waits_for_resume_headroom(tmp_path, monkeypatch):
    """The weekly allowance grows continuously, so a box running at pace
    crosses zero headroom back and forth; the resume ping (and the marker
    removal) waits for RESUME_HEADROOM or every crossing pings a pair."""
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    marker = Path(c.state_dir) / "budget-stalled"
    patch_usage(monkeypatch, util=0.95)
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_stall") == 1 and marker.exists()
    patch_usage(monkeypatch, util=0.79)      # admitted, session headroom 0.01
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_resume") == 0 and marker.exists()
    patch_usage(monkeypatch, util=0.77)      # admitted, session headroom 0.03
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_resume") == 1 and not marker.exists()
    assert d.notifier.sent.count("budget_stall") == 1


def test_woken_pr_open_task_is_gated_on_the_model_it_spawns(tmp_path):
    """A parked pr-open task resumes as a fresh address-review round, which
    runs on the implement entry — that is the model the gate must ask, not
    whatever the pr-open stage itself would (not) resolve to."""
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PR_OPEN, status=waking(), pr_number=7,
              track="deep")
    opus_over_pace = lambda m: DENY_ALL(m) if "opus" in m else ADMIT_ALL(m)  # noqa: E731
    d = deps()
    main._drive_wakes(c, d, admit=opus_over_pace, order=tuple)
    assert d.sessions.spawned == [] and d.sessions.resumed == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    assert [s[:3] + (s[4],) for s in d.sessions.spawned] == [
        (42, Stage.ADDRESS_REVIEW.value, "anthropic/claude-opus-5", "medium")]


def test_operator_can_bypass_usage_gate_for_one_resume(tmp_path):
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "plan"))
    make_task(c, issue=42, stage=Stage.PLAN, status=waking(), resume_bypass_usage=True)
    d = deps()
    main._drive_wakes(c, d, admit=DENY_ALL, order=tuple)
    assert d.sessions.resumed == [(42, "Continue.", "anthropic/claude-opus-5", "")]
    saved = load(c.state_dir, "portfolio_eval", 42)
    assert saved.status.kind is Kind.RUNNING
    assert saved.resume_bypass_usage is False
    assert saved.resume_model_override == ""


def test_operator_can_resume_with_another_configured_model(tmp_path):
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "plan"))
    make_task(c, issue=42, stage=Stage.PLAN, status=waking(), resume_model_override="claude-sonnet-4-6")
    d = deps()
    admit_sonnet = lambda model: ADMIT_ALL(model) if "sonnet" in model else DENY_ALL(model)  # noqa: E731
    main._drive_wakes(c, d, admit=admit_sonnet, order=tuple)
    assert d.sessions.resumed == [
        (42, "Continue.", "anthropic/claude-sonnet-4-6", "")]


def test_resume_override_waits_for_capacity_without_losing_choice(tmp_path):
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=41)
    make_task(c, issue=42, status=waking(),
              resume_model_override="claude-sonnet-4-6",
              resume_bypass_usage=True)
    main._drive_wakes(c, deps(), admit=DENY_ALL, order=tuple)
    saved = load(c.state_dir, "portfolio_eval", 42)
    assert saved.status.kind is Kind.WAKING
    assert saved.resume_model_override == "claude-sonnet-4-6"
    assert saved.resume_bypass_usage is True


def test_forced_active_task_uses_choice_at_next_stage(tmp_path):
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.SPEC)
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "spec", "status": "done", "note": "",
        "artifact": "spec.md",
         "track": "standard"}))
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(
            model="claude-sonnet-4-6", bypass_usage=True))
    sess = FakeSessions(alive=(42,))

    drive._drive_task(c, deps(sess=sess), c.repositories[0],
                     load(c.state_dir, "portfolio_eval", 42), DENY_ALL, tuple)

    assert [spawn[:3] for spawn in sess.spawned] == [
        (42, Stage.PLAN.value, "anthropic/claude-sonnet-4-6")]
    assert execution_overrides.load(
        c.state_dir, "portfolio_eval", 42) is None


def test_failed_launch_keeps_the_operators_one_time_choice(tmp_path):
    """The choice is for the launch that runs: one that failed has not used it."""
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.SPEC)
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "spec", "status": "done", "note": "", "artifact": "spec.md",
        "track": "standard"}))
    choice = execution_overrides.ExecutionOverride(
        model="claude-sonnet-4-6", bypass_usage=True)
    execution_overrides.save(c.state_dir, "portfolio_eval", 42, choice)

    drive._drive_task(c, deps(sess=FakeSessions(alive=(42,), spawn_raises={42})),
                     c.repositories[0], load(c.state_dir, "portfolio_eval", 42),
                     DENY_ALL, tuple)

    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    assert execution_overrides.load(c.state_dir, "portfolio_eval", 42) == choice


def test_resume_uses_the_model_for_the_parked_stage(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, stage=Stage.IMPLEMENT, status=waking())
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [(issue, text.split("\n\n")[0], model, effort)
            for issue, text, model, effort in sess.resumed] == [(42, "Continue.", "anthropic/claude-opus-5", "")]


def test_models_log_appends_one_line_per_spawn(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.SPEC)
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md", "track": "standard"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    log = (wt / ".agent" / "models.log").read_text().strip().splitlines()
    assert len(log) == 1
    assert log[0].endswith(" plan anthropic/claude-opus-5")


def test_status_lines_carry_the_resolved_model(tmp_path):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, repository="portfolio_eval", stage=Stage.IMPLEMENT, slot=0,
        worktree="/wt", branch="agent/task-42", title="Fix rounding",
        updated_at="2026-07-24T00:00:00+00:00", track="standard"))
    save(c.state_dir, TaskState(
        issue=43, repository="portfolio_eval", stage=Stage.SPEC, slot=1,
        worktree="/wt2", branch="agent/task-43", title="New chart",
        updated_at="2026-07-24T00:00:00+00:00", track="standard"))
    lines = main._status_lines(c)
    assert lines[0] == "#42 Fix rounding — implement [anthropic/claude-opus-5] (slot 0)"
    assert lines[1] == "#43 New chart — spec [anthropic/claude-opus-5] (slot 1)"


def _plan_on_sonnet(c):
    """cfg whose standard track plans on a model no other stage uses, so a
    gate task's model can only have come from the plan list."""
    stages = {s: ["claude-opus-5"] for s in ("spec", "implement", "review")}
    return dc_replace(c, models=parse_policy({
        "triage": ["claude-sonnet-5@low"], "untracked": "standard",
        "tracks": {"standard": {"when": "x", "plan": ["claude-sonnet-5"], **stages}}}))


def test_status_line_for_task_parked_at_plan_review_shows_the_plan_model(tmp_path):
    """Finding A: a task lingering at the plan-review gate is still running
    its PLAN session — /status must show that stage's model."""
    c = _plan_on_sonnet(cfg(tmp_path))
    save(c.state_dir, TaskState(
        issue=42, repository="portfolio_eval", stage=Stage.PLAN, status=held(),
        slot=0, worktree="/wt", branch="agent/task-42", title="New chart",
        updated_at="2026-07-24T00:00:00+00:00", track="standard"))
    lines = main._status_lines(c)
    assert lines[0] == ("#42 New chart — plan "
                        "[anthropic/claude-sonnet-5] [held-plan-approval] (slot 0)")


def test_resume_at_plan_review_gate_uses_the_plan_model(tmp_path, monkeypatch):
    """Finding A: resuming a task parked at the plan-review gate must run the
    same model as the plan session that waited there, not the policy
    default that `stage.value` ('awaiting-plan-review') falls through to."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = _plan_on_sonnet(cfg(tmp_path))
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "plan"))
    make_task(c, issue=42, stage=Stage.PLAN, status=waking())
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == [(42, "Continue.", "anthropic/claude-sonnet-5", "")]


def test_status_lines_survive_a_task_whose_repository_is_gone(tmp_path):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=44, repository="retired_repository", stage=Stage.PLAN, slot=0,
        worktree="/wt", branch="agent/task-44", title="Orphan",
        updated_at="2026-07-24T00:00:00+00:00", track="standard"))
    lines = main._status_lines(c)
    assert "[anthropic/claude-opus-5]" in lines[0]   # falls back to the global policy


def test_orphaned_task_at_the_plan_review_gate_still_maps_to_the_plan_model(
        tmp_path):
    # The global-policy fallback must go through the same stage mapping as the
    # normal path, or a task parked at the gate misreports its model.
    c = _plan_on_sonnet(cfg(tmp_path))
    save(c.state_dir, TaskState(
        issue=45, repository="retired_repository", stage=Stage.PLAN, status=held(),
        slot=0, worktree="/wt", branch="agent/task-45", title="Orphan chart",
        updated_at="2026-07-24T00:00:00+00:00", track="standard"))
    lines = main._status_lines(c)
    assert lines[0] == ("#45 Orphan chart — plan "
                        "[anthropic/claude-sonnet-5] [held-plan-approval] (slot 0)")


def test_status_line_for_a_parked_task_puts_model_before_park(tmp_path):
    """Finding C: the format is
    '#{issue} {title} — {stage} [{model}] [{status}] (slot {slot})' — the
    [model] segment must come before the status segment; a parked task holds
    no slot."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, repository="portfolio_eval", stage=Stage.IMPLEMENT, status=human(), slot=0,
        worktree="/wt", branch="agent/task-42", title="Fix rounding",
        updated_at="2026-07-24T00:00:00+00:00", track="standard"))
    lines = main._status_lines(c)
    assert lines[0] == ("#42 Fix rounding — implement [anthropic/claude-opus-5] "
                        "[parked-human] (no slot)")
