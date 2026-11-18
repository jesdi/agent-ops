"""Claiming: claim rounds, quarantine, re-ranking of ended tasks, provisioning
failures and the claim commit (dispatcher/claiming.py)."""
import json
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import failures
from dispatcher.github import Candidate
from dispatcher.state import (NO_SLOT, Stage, TaskState, load, load_all, save)
from tests.configfakes import with_project
from tests.dispatchfakes import (ADMIT_ALL, FakeGitHub, FakeSessions,
                                 cfg, deps, make_task, patch_usage,
                                 patch_workspace, pr_open_task, _events)
from tests.statuses import CANCELED, DONE, review, waking, failed
from dispatcher.status import Kind
from dispatcher import claiming, eventlog


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def test_new_candidate_claimed_and_spec_spawned(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))

    assert gh.claimed == [42]
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "anthropic/claude-opus-5")]
    ts = load(c.state_dir, "portfolio_eval", 42)
    assert ts.stage is Stage.SPEC and ts.slot == 0 and ts.title == "Add widget"
    sig = json.loads(
        (Path(c.repositories[0].worktrees_path) / "task-42" / ".agent" / "stage.json")
        .read_text())
    assert sig == {"stage": "spec", "status": "working",
                   "model": "anthropic/claude-opus-5", "effort": ""}


def test_no_claim_while_a_task_file_cannot_be_read(tmp_path, monkeypatch):
    """A task in a shape the loader refuses may hold the very issue a claim
    would take again, and a unit and a slot no count can see: the claim
    round waits until the file is readable (migrated or repaired)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    (Path(c.state_dir) / "task-portfolio_eval-42.json").write_text(json.dumps(
        {"issue": 42, "repository": "portfolio_eval", "stage": "awaiting-plan-review",
         "slot": 0, "worktree": "/wt", "branch": "agent/task-42", "title": "t",
         "updated_at": "2026-07-21T00:00:00+00:00"}))
    gh = FakeGitHub([Candidate(42, "Add widget", "u42"), Candidate(43, "Other", "u43")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []


def test_claim_snapshots_effort_and_labels(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=3,
                               labels=("auto", "frontend"))])
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.effort == 3
    assert t.labels == ("auto", "frontend")


def test_capacity_blocks_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    for i, issue in enumerate([1, 2, 3]):
        wt = Path(c.repositories[0].worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True)
        save(c.state_dir, TaskState(issue=issue, repository="portfolio_eval",
                                    stage=Stage.SPEC, slot=i, worktree=str(wt),
                                    branch=f"agent/task-{issue}", title="t",
                                    updated_at="2026-07-14T00:00:00+00:00",
                                    track="standard"))
    gh = FakeGitHub([Candidate(99, "X", "u")])
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2, 3})))
    assert gh.claimed == []


def test_slot_exhaustion_stops_the_claim_round(tmp_path, monkeypatch):
    """Box-wide capacity is free (free > 0), but allocate_slot has nothing to
    hand out — the round must stop rather than claim a candidate it cannot
    give a slot to."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    monkeypatch.setattr(claiming, "allocate_slot", lambda *a, **kw: None)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])

    claiming._claim_new(c, deps(gh), c.repositories, ADMIT_ALL, tuple, False)

    assert gh.claimed == []


def test_active_task_for_an_unconfigured_repository_does_not_break_the_round(
        tmp_path, monkeypatch):
    """A repository that has since left targets.yaml can still have an
    in-flight task on disk. _round_counts must not crash looking it up, and
    the round must still claim normally for the repositories still configured."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    orphan_wt = Path(c.repositories[0].worktrees_path) / "task-orphan-1"
    (orphan_wt / ".agent").mkdir(parents=True, exist_ok=True)
    save(c.state_dir, TaskState(
        issue=1, repository="retired_repository", stage=Stage.IMPLEMENT, slot=0,
        worktree=str(orphan_wt), branch="agent/task-1", title="t",
        updated_at="2026-07-21T00:00:00+00:00", track="standard"))
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])

    claiming._claim_new(c, deps(gh), c.repositories, ADMIT_ALL, tuple, False)

    assert gh.claimed == [42]


def test_workspace_failure_reports_quarantines_and_pass_survives(tmp_path, monkeypatch):
    """create_workspace failure: no claim, no state file, one issue + one
    ping, quarantine written, and the pass exits cleanly (regression: one
    bad candidate killed the pass). Capped at one provisioning failure per
    repository per pass, so the NEXT candidate (43) is NOT claimed this pass —
    a systemic fault is far likelier than a per-candidate one, and the next
    pass retries 43."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Bad", "u42"), Candidate(43, "Good", "u43")])
    sess = FakeSessions()

    def failing_workspace(repository, issue, base="main", dry_run=False, auth=None):
        if issue == 42:
            raise RuntimeError("pipenv: no python 3.13")
        wt = Path(repository.worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        return str(wt)

    monkeypatch.setattr(claiming, "create_workspace", failing_workspace)
    d = deps(gh, sess)
    main.run_pass(c, d)  # must NOT raise

    assert gh.claimed == [], "capped at one provisioning failure per pass; 43 not claimed this pass"
    assert load(c.state_dir, "portfolio_eval", 42) is None, "no state file for the failed candidate"
    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/agent-ops" and "provisioning" in title
    assert "no python 3.13" in body
    assert d.notifier.sent.count("task_failed") == 1
    rec = json.loads(failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).read_text())
    assert rec["blocker_issue"] == 501 and rec["blocker_repo"] == "jesdi/agent-ops"


def test_workspace_failure_second_pass_dedupes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Bad", "u42")])

    def failing_workspace(repository, issue, base="main", dry_run=False, auth=None):
        raise RuntimeError("pipenv: no python 3.13")

    monkeypatch.setattr(claiming, "create_workspace", failing_workspace)
    d = deps(gh, FakeSessions())
    main.run_pass(c, d)
    # Simulate a force-retry (record deleted) with the same failure: the
    # fingerprint dedupes, and the quarantine is rewritten with the
    # ORIGINAL blocker number read back from the marker.
    failures.quarantine_path(c.state_dir, "portfolio_eval", 42).unlink()
    main.run_pass(c, d)
    assert len(gh.created_issues) == 1
    assert d.notifier.sent.count("task_failed") == 1
    rec = json.loads(failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).read_text())
    assert rec["blocker_issue"] == 501


def test_quarantined_candidate_with_open_blocker_skipped(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Bad", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []


def test_closed_blocker_unquarantines_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Fixed now", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "CLOSED"})
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [42], "candidate claimable in the SAME pass"
    assert not failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).exists()


def test_issue_state_error_keeps_candidate_quarantined(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Bad", "u42"), Candidate(43, "Good", "u43")],
                    issue_state_raises=True)
    main.run_pass(c, deps(gh, FakeSessions()))  # must not raise
    assert gh.claimed == [43]


def test_quarantine_verdict_tracks_an_open_blocker_and_pass_heartbeat(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Blocked", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})

    main.run_pass(c, deps(gh, FakeSessions()))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": heartbeat["started_at"],
                       "skipped": [["portfolio_eval", 42]]}
    assert gh.claimed == []


def test_quarantine_verdict_clears_a_closed_blocker_same_pass(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    verdict_path = Path(c.state_dir) / "quarantine-verdict.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    verdict_path.write_text(json.dumps({
        "pass_started": "2026-11-15T09:50:00+00:00",
        "skipped": [["portfolio_eval", 42]],
    }))
    gh = FakeGitHub([Candidate(42, "Unblocked", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "CLOSED"})

    main.run_pass(c, deps(gh, FakeSessions()))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(verdict_path.read_text())
    assert verdict == {"pass_started": heartbeat["started_at"], "skipped": []}
    assert gh.claimed == [42]
    assert not failures.quarantine_path(c.state_dir, "portfolio_eval", 42).exists()


def test_quarantine_verdict_lists_a_record_without_a_blocker(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    no_blocker = failures.quarantine_path(c.state_dir, "portfolio_eval", 42)
    no_blocker.parent.mkdir(parents=True, exist_ok=True)
    no_blocker.write_text(json.dumps({
        "task_issue": 42, "blocker_repo": "jesdi/agent-ops",
        "blocker_issue": 0, "fingerprint": "abc", "created_at": "now",
    }))
    gh = FakeGitHub([Candidate(42, "No blocker reference", "u42")])

    main.run_pass(c, deps(gh, FakeSessions()))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": heartbeat["started_at"],
                       "skipped": [["portfolio_eval", 42]]}
    assert gh.claimed == []


def test_quarantine_verdict_lists_unreadable_and_failed_lookup_candidates(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    alpha = dc_replace(
        c.repositories[0], name="alpha", repo="jesdi/alpha",
        clone_path=str(tmp_path / "alpha"),
        worktrees_path=str(tmp_path / "alpha.worktrees"),
        project=dc_replace(c.repositories[0].project, name="alpha"))
    beta = dc_replace(
        c.repositories[0], name="beta", repo="jesdi/beta",
        clone_path=str(tmp_path / "beta"),
        worktrees_path=str(tmp_path / "beta.worktrees"),
        project=dc_replace(c.repositories[0].project, name="beta"))
    c = dc_replace(c, repositories=[alpha, beta], capacity=2)
    bad_record = failures.quarantine_path(c.state_dir, "alpha", 73)
    bad_record.parent.mkdir(parents=True, exist_ok=True)
    bad_record.write_text("{")
    failures.write_quarantine(c.state_dir, "beta", 12,
                              "jesdi/agent-ops", 502, "def")
    gh = FakeGitHub(
        issue_state_raises=True,
        cands_by_repository={
            "alpha": [Candidate(73, "Unreadable quarantine", "u73")],
            "beta": [Candidate(12, "Failed blocker lookup", "u12")],
        })

    main.run_pass(c, deps(gh, FakeSessions()))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": heartbeat["started_at"],
                       "skipped": [["alpha", 73], ["beta", 12]]}
    assert gh.claimed == []


def test_quarantine_verdict_is_written_when_capacity_is_full(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    for slot, issue in enumerate((1, 2, 3)):
        wt = Path(c.repositories[0].worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True)
        save(c.state_dir, TaskState(
            issue=issue, repository="portfolio_eval", stage=Stage.SPEC,
            slot=slot, worktree=str(wt), branch=f"agent/task-{issue}",
            title="t", updated_at="2026-07-14T00:00:00+00:00",
            track="standard"))
    gh = FakeGitHub([Candidate(99, "No room", "u99")])

    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2, 3})))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": heartbeat["started_at"], "skipped": []}
    assert gh.claimed == []


def test_quarantine_verdict_is_written_when_slot_allocation_exhausts(
        tmp_path, monkeypatch):
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 73,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub(
        [Candidate(73, "Blocked", "u73"), Candidate(74, "No slot", "u74")],
        issue_states={("jesdi/agent-ops", 501): "OPEN"})
    monkeypatch.setattr(claiming, "allocate_slot", lambda *a, **kw: None)
    pass_started = "2026-11-15T10:00:00+00:00"

    claiming._claim_new(c, deps(gh), c.repositories, ADMIT_ALL, tuple, False,
                    pass_started)

    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": pass_started,
                       "skipped": [["portfolio_eval", 73]]}
    assert gh.claimed == []


def test_quarantine_verdict_is_written_when_no_project_remains_to_pick(
        tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[with_project(
        c.repositories[0], max_active=1)], capacity=2)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 73,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub(
        [Candidate(73, "Blocked", "u73"), Candidate(74, "Claimed", "u74")],
        issue_states={("jesdi/agent-ops", 501): "OPEN"})
    pass_started = "2026-11-15T10:00:00+00:00"

    claiming._claim_new(c, deps(gh), c.repositories, ADMIT_ALL, tuple, False,
                    pass_started)

    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": pass_started,
                       "skipped": [["portfolio_eval", 73]]}
    assert gh.claimed == [74]


def test_quarantine_verdict_write_failure_warns_without_breaking_pass(
        tmp_path, monkeypatch, capsys):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    verdict_path = Path(c.state_dir) / "quarantine-verdict.json"
    verdict_path.mkdir(parents=True)

    d = deps()
    main.run_pass(c, d)

    captured = capsys.readouterr()
    assert "[warn]" in captured.err and "quarantine verdict" in captured.err.lower()
    assert (Path(c.state_dir) / "pass.json").is_file()


def test_claims_paused_keeps_the_verdict_file_stale_and_untrusted(
        tmp_path, monkeypatch):
    from dispatcher import triage
    from web.sources import Sources

    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    verdict_path = Path(c.state_dir) / "quarantine-verdict.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    prior = (b'{"pass_started":"2026-11-15T09:50:00+00:00",'
             b'"skipped":[["portfolio_eval",42]]}')
    verdict_path.write_bytes(prior)
    (Path(c.state_dir) / triage.REQUEST_FILE).write_text(
        '{"requested_at":"2026-11-15T09:50:00+00:00"}')
    # The request is still waiting for capacity: tick neither launches nor
    # expires it, so the pass sees claims paused.
    monkeypatch.setattr(triage, "tick", lambda *_args: None)

    d = deps()
    main.run_pass(c, d)

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    assert verdict_path.read_bytes() == prior
    assert heartbeat["started_at"] != "2026-11-15T09:50:00+00:00"
    assert Sources(c.state_dir, d.sessions, d.github).quarantine_verdict(
        heartbeat) == frozenset()


def test_run_pass_verdict_is_readable_against_its_pass_heartbeat(
        tmp_path, monkeypatch):
    from web.sources import Sources

    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Blocked", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})

    d = deps(gh, FakeSessions())
    main.run_pass(c, d)

    sources = Sources(c.state_dir, d.sessions, d.github)
    assert sources.quarantine_verdict(sources.pass_heartbeat()) == frozenset({
        ("portfolio_eval", 42)})


def test_release_called_when_claim_fails(tmp_path, monkeypatch):
    """github.claim failure after workspace+save must trigger release and leave QUEUED state (regression: orphan-claim window)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)

    class ClaimFailsGitHub(FakeGitHub):
        def claim(self, repository, cand):
            raise RuntimeError("GitHub API error")

    gh = ClaimFailsGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()

    with pytest.raises(RuntimeError):
        main.run_pass(c, deps(gh, sess))

    assert any(issue == 42 for issue, _ in gh.released), "release must be called for the issue when claim fails"
    ts = load(c.state_dir, "portfolio_eval", 42)
    assert ts is not None and ts.stage is Stage.QUEUED, "QUEUED state file must exist for crash-recovery path"


def test_claim_and_spawn_append_claimed_and_stage_started_events(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    main.run_pass(c, deps(FakeGitHub([Candidate(42, "Add widget", "u42")])))
    events = eventlog.read_tail(c.state_dir)
    assert [(e["event"], e["issue"]) for e in events] == [
        ("claimed", 42), ("stage-started", 42)]
    assert events[0]["repository"] == "portfolio_eval"
    assert events[1]["stage"] == "spec"
    assert events[1]["model"] == "anthropic/claude-opus-5"


def test_reopened_canceled_issue_is_claimed_again(tmp_path, monkeypatch):
    # Won't-do is reversible: the operator reopens the issue and moves the
    # card back to Ready, so it ranks as a candidate again (rank drops CLOSED
    # issues — a candidate row proves the reopen). The stale CANCELED
    # tombstone must be swept and the issue claimed fresh.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PLAN, status=CANCELED, slot=NO_SLOT,
              updated_at="2026-07-21T00:00:00+00:00")  # canceled long ago
    gh = FakeGitHub([Candidate(42, "Conditions changed", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [42]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is not Kind.CANCELED
    reopened = [e for e in eventlog.read_tail(c.state_dir)
                if e["event"] == "reopened"]
    assert reopened and reopened[0]["issue"] == 42
    claimed = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "claimed"]
    assert claimed and claimed[0]["stage"] == "queued"


@pytest.mark.parametrize("reason", ["pr-closed", "unknown"])
def test_failed_queued_task_ranked_again_is_swept_and_claimed(tmp_path, monkeypatch, reason):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED, status=failed(reason), slot=NO_SLOT,
              updated_at="2026-07-21T00:00:00+00:00")
    old = load(c.state_dir, "portfolio_eval", 42)
    gh = FakeGitHub([Candidate(42, "Ranked again", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [42]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is not Kind.FAILED and t.title != old.title
    events = eventlog.read_tail(c.state_dir)
    reopened = [e for e in events if e["event"] == "reopened"]
    assert [e["issue"] for e in reopened] == [42]
    assert reopened[0]["detail"] == "failed task ranked again"
    claimed = [e for e in events if e["event"] == "claimed"]
    assert claimed and claimed[0]["stage"] == "queued"


def test_canceled_this_pass_ranks_without_sweeping_its_tombstone(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PLAN, status=CANCELED, slot=NO_SLOT,
              updated_at="2999-01-01T00:00:00+00:00")   # written after the pass started
    gh = FakeGitHub([Candidate(42, "Still on board", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.CANCELED
    assert not [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "reopened"]


def test_failed_task_ranked_again_wakes_in_place(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    made = []
    monkeypatch.setattr(claiming, "create_workspace", lambda *a, **k: made.append(a))
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, status=failed("task-crash"), slot=NO_SLOT,
              updated_at=_ago(minutes=1))
    gh = FakeGitHub([Candidate(42, "Failure issue closed", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status == waking(failed("task-crash"), "ranked")
    assert t.stage is Stage.REVIEW
    assert gh.claimed == [] and made == []
    assert [x.issue for x in load_all(c.state_dir)] == [42]


def test_failed_this_pass_ranks_without_waking(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, status=failed("task-crash"), slot=NO_SLOT,
              updated_at="2999-01-01T00:00:00+00:00")
    gh = FakeGitHub([Candidate(42, "Still on board", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert load(c.state_dir, "portfolio_eval", 42).status == failed("task-crash")
    assert gh.claimed == []


def test_done_task_ranked_again_is_rejected(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PR_OPEN, status=DONE, slot=NO_SLOT,
              updated_at=_ago(minutes=1))
    gh = FakeGitHub([Candidate(42, "Merged but Ready", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.DONE
    rejected = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "transition-rejected"]
    assert rejected and rejected[0]["transition"]["event"] == "candidate-ranked"


def test_done_task_ranked_every_pass_is_rejected_once(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PR_OPEN, status=DONE, slot=NO_SLOT,
              updated_at=_ago(minutes=1))
    gh = FakeGitHub([Candidate(42, "Merged but Ready", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    main.run_pass(c, deps(gh, FakeSessions()))
    assert len(_events(c, "transition-rejected")) == 1
    t = load(c.state_dir, "portfolio_eval", 42)
    save(c.state_dir, dc_replace(t, updated_at=_ago(seconds=1)))  # the task changed
    main.run_pass(c, deps(gh, FakeSessions()))
    assert len(_events(c, "transition-rejected")) == 2


def test_claims_stop_at_max_open_even_with_capacity_free(tmp_path, monkeypatch):
    """Parked and pr-open tasks hold no capacity but still hold a
    worktree; max_open counts them so the box stops claiming."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), max_open=2)
    make_task(c, issue=1, stage=Stage.PLAN, status=review(), slot=NO_SLOT)
    pr_open_task(c, issue=2)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert sess.spawned == []


# ---------------------------------------------------------------------------
# --dry-run must not touch local state
#
# Every I/O *dependency* was already stubbed under --dry-run (GitHubClient,
# Sessions, Notifier, remove_workspace), but the local writes — state files,
# events.jsonl, and the markers/caches that live beside them — ran for real.
# A dry run therefore advanced tasks to a terminal stage and wrote history
# while every visible side effect printed "[dry-run] …" and was skipped.
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write_state_for_a_newly_claimed_task(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    main.run_pass(c, deps(gh), dry_run=True)

    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert eventlog.read_tail(c.state_dir) == []
