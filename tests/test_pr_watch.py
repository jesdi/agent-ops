"""The PR and CI watch: merges, closes, feedback, red checks, conflicts and
CI-parked runs, read each pass and fed to the lifecycle table."""
import json
import subprocess
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import eventlog, pr_poll, pr_watch
from dispatcher.board import BoardError
from dispatcher.github import Candidate
from dispatcher.pr_poll import CIStatus
from dispatcher.state import (NO_SLOT, LoopCaps, Stage, load, save, SessionRecord, write_session)
from tests.dispatchfakes import (FakeGitHub, FakeNotifier, FakeSessions,
                                 cfg, deps, make_task, patch_teardown, patch_usage,
                                 patch_workspace, payload, pr_open_task, replace_capacity, _events, feedback_pending)
from tests.statuses import RUNNING, PR, DONE, human, ci, waking, failed
from dispatcher.status import Cause, HumanReason, Kind
from dispatcher.state import operator_request


def test_ci_completion_marks_unpark_requested(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, status=ci(4242))
    gh = FakeGitHub(run_conclusion="failure")
    # No free capacity race: resumed same pass is fine — assert either state.
    main.run_pass(c, deps(gh, FakeSessions()))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind in (Kind.WAKING, Kind.RUNNING)  # woken; may already have resumed
    from dispatcher import messages
    # Message may be delivered already if resumed same pass; all_messages captures both.
    assert "run 4242 concluded: failure" in messages.all_messages(
        c.state_dir, "portfolio_eval", 42)[0].text


def test_run_status_error_does_not_abort_pass(tmp_path, monkeypatch):
    """A gh run_status failure must be treated as 'still running': skip that task,
    keep the pass going (e.g., a second claimable candidate is still claimed)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    # Parked CI task whose run_status call will raise CalledProcessError
    make_task(c, issue=42, status=ci(4242))
    # A fresh candidate available to be claimed
    gh = FakeGitHub(cands=[Candidate(99, "fresh", "u")], run_status_raises=True)
    sess = FakeSessions()
    # Must NOT raise; the pass must survive the error
    main.run_pass(c, deps(gh, sess))
    # CI-parked task remains unchanged (still PARK_CI with same run_id)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_CI and t.status.run_id == 4242
    # The rest of the pass continued: the fresh candidate was claimed
    assert gh.claimed == [99]


def test_merged_pr_moves_task_to_done(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    t = pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    notif = FakeNotifier()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess=sess, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.status.kind is Kind.DONE and got.done_at
    assert (42, "Done") in gh.statused
    assert 42 in sess.ended
    assert removed == [(t.worktree, t.branch, False)]
    assert (c.repositories[0].repo, t.branch) in gh.deleted_branches
    assert "task_done" in notif.sent


def test_merged_on_a_board_without_done_finishes_the_task_with_a_warning(
        tmp_path, monkeypatch, capsys):
    """No Done option is the old empty-id opt-out: the board is skipped,
    loudly, and the merged task still finishes (a retry could never pass)."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)

    class NoDone(FakeGitHub):
        def set_status(self, repository, issue, status):
            raise BoardError("project jesdi/1: field 'Status' has no option 'Done'")

    gh = NoDone()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh))  # must not raise
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.DONE
    assert len(removed) == 1
    assert "has no option 'Done'" in capsys.readouterr().err


def test_closed_unmerged_pr_fails_task_preserving_worktree(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    notif = FakeNotifier()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    assert removed == [] and "pr_closed" in notif.sent
    assert sess.ended == [42]   # the close ends whatever session is left


def test_feedback_sets_pending_flag_and_notifies_once(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 0)  # full: spawn deferred
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{
        "createdAt": "2026-07-30T09:00:00Z", "author": {"login": "alice"}}])
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert feedback_pending(got) is True and got.stage is Stage.PR_OPEN
    assert got.feedback_cursor == ""  # cursor NOT advanced here; that is Task 9's job
    assert notif.sent.count("pr_feedback") == 1
    main.run_pass(c, deps(gh, notifier=notif))   # second pass: no re-notify
    assert notif.sent.count("pr_feedback") == 1


def test_own_and_bot_comments_do_not_reopen(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[
        {"createdAt": "2026-07-30T09:00:00Z", "author": {"login": "agent-bot"}},
        {"createdAt": "2026-07-30T09:01:00Z",
         "author": {"login": "github-actions[bot]"}}])
    main.run_pass(c, deps(gh))
    assert feedback_pending(load(c.state_dir, "portfolio_eval", 42)) is False


def test_pr_number_resolved_from_branch_when_missing(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, pr_number=0)
    gh = FakeGitHub()
    gh.branch_prs["agent/task-42"] = 12
    gh.pr_payloads[12] = payload()
    main.run_pass(c, deps(gh))
    assert load(c.state_dir, "portfolio_eval", 42).pr_number == 12


def test_poll_failure_leaves_task_untouched(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_view_raises = True
    main.run_pass(c, deps(gh))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN and feedback_pending(got) is False


def test_merge_board_failure_leaves_task_pr_open_and_pass_survives(
        tmp_path, monkeypatch):
    """set_status raising mid-FinishMerged must be caught per-task: the pass
    continues (no exception escapes run_pass), the task stays PR_OPEN (state
    flip never reached), and the worktree was NOT removed (teardown runs after
    the raising step so it too was never reached)."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)

    class SetStatusFails(FakeGitHub):
        def set_status(self, repository, issue, status):
            raise subprocess.CalledProcessError(1, ["gh"])

    gh = SetStatusFails()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh))  # must not raise
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN  # state flip never reached
    assert removed == []               # teardown never reached (board runs first)


def test_closed_pr_on_a_running_address_review_ends_its_session_failed(
        tmp_path, monkeypatch):
    """The close ends the live address-review pane and frees its slot."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.ADDRESS_REVIEW, status=RUNNING, slot=0, pr_number=12)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    notif, sess = FakeNotifier(), FakeSessions(alive={42})
    main.run_pass(c, deps(gh, sess, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.stage, got.status, got.slot) == (
        Stage.ADDRESS_REVIEW, failed("pr-closed"), NO_SLOT)
    assert 42 in sess.ended and notif.sent.count("pr_closed") == 1


def test_closed_pr_on_a_waking_task_ends_it_failed(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 0)     # the wake cannot launch
    pr_open_task(c, status=waking(PR, "attention(feedback)"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.stage, got.status) == (Stage.PR_OPEN, failed("pr-closed"))
    assert sess.spawned == []


def test_merge_whose_teardown_fails_halfway_stays_parked_and_retries(
        tmp_path, monkeypatch):
    """The teardown runs before the commit: a raise leaves parked(pr), and
    the PR, still merged, finishes the task next pass."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)

    class DeleteFailsOnce(FakeGitHub):
        def delete_branch(self, repository, branch):
            if not self.deleted_branches:
                self.deleted_branches.append(None)
                raise subprocess.CalledProcessError(1, ["git"])
            super().delete_branch(repository, branch)

    gh = DeleteFailsOnce()
    gh.pr_payloads[12] = payload(state="MERGED", merged_at="2026-07-30T10:00:00Z")
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, notifier=notif))  # must not raise
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.status, got.done_at, len(removed)) == (PR, "", 1)
    assert "task_done" not in notif.sent and _events(c, "merged") == []
    main.run_pass(c, deps(gh, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.status == DONE and got.done_at
    assert notif.sent.count("task_done") == 1 and len(_events(c, "merged")) == 1


def test_merge_after_an_exhausted_park_ends_done(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=human("loop-cap", "ci loop hit its cap"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED", merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.stage, got.status) == (Stage.PR_OPEN, DONE) and got.done_at


class _RealPanes(FakeSessions):
    """Panes as podman keeps them: ending one kills it, and a launch beside a
    live one is refused (sessions._free_name)."""

    def end(self, repository, issue):
        super().end(repository, issue)
        self.alive_set.discard(issue)

    def spawn_stage(self, repository, issue, *a, **kw):
        if issue in self.alive_set:
            raise RuntimeError(f"container for #{issue} is still running")
        super().spawn_stage(repository, issue, *a, **kw)
        self.alive_set.add(issue)


def test_merge_during_address_review_whose_teardown_fails_finishes_next_pass(
        tmp_path, monkeypatch):
    """The teardown runs before the pane is ended: a raise leaves the
    address-review session alive, so the next pass finds no crash and the
    still-merged PR finishes the task."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.ADDRESS_REVIEW, status=RUNNING, slot=0, pr_number=12)

    class CloseFailsOnce(FakeGitHub):
        failed = False

        def close_issue(self, repo, issue):
            if not self.failed:
                self.failed = True
                raise subprocess.CalledProcessError(1, ["gh"])
            super().close_issue(repo, issue)

    gh = CloseFailsOnce()
    gh.pr_payloads[12] = payload(state="MERGED", merged_at="2026-07-30T10:00:00Z")
    notif, sess = FakeNotifier(), _RealPanes(alive={42})
    main.run_pass(c, deps(gh, sess, notifier=notif))
    main.run_pass(c, deps(gh, sess, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.status, got.slot) == (DONE, NO_SLOT) and got.done_at
    assert len(removed) == 1 and sess.ended == [42]
    assert "session_crashed" not in notif.sent and notif.sent.count("task_done") == 1


def test_feedback_on_a_parked_pr_with_a_live_pane_ends_it_before_address_review(
        tmp_path, monkeypatch):
    """A task parked at pr-open before the pr-open transition ended its pane
    still has a live review session: the address-review launch ends it."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=PR)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{"createdAt": "2026-09-07T09:00:00Z",
                                           "author": {"login": "alice"}}])
    notif, sess = FakeNotifier(), _RealPanes(alive={42})
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status) == (Stage.ADDRESS_REVIEW, RUNNING)
    assert sess.ended == [42] and sess.spawned[-1][1] == "address-review"


def test_conflicts_past_the_cap_park_the_pr_open_task_on_a_human(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(ci=1))
    pr_open_task(c, ci_rounds=1)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="abc")
    notif, sess = FakeNotifier(), FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind, t.conflict_cursor) == (
        Stage.PR_OPEN, Kind.PARKED_HUMAN, "abc")
    assert t.status.reason is HumanReason.LOOP_CAP
    assert "parked_question" in notif.sent and not sess.spawned


def merged_cfg(tmp_path):
    return cfg(tmp_path)


def test_dry_run_pass_does_not_tear_down_a_merged_task(tmp_path, monkeypatch):
    """--dry-run must reach remove_workspace as a dry run, not as a real
    deletion: GitHubClient and Sessions already no-op under dry_run, so the
    worktree/branch removal was the one side effect that still executed for
    real while the operator watched `[dry-run] delete_branch …` scroll by."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    t = pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)
    assert removed == [(t.worktree, t.branch, True)]


def test_remove_workspace_dry_run_flag_reaches_the_real_teardown(
        tmp_path, monkeypatch, capsys):
    """Same path with the real workspace module in place: nothing shells
    out. Guards the signature itself — a remove_workspace that ignored
    dry_run would still run git here."""
    patch_usage(monkeypatch)
    from dispatcher import workspace as workspace_mod
    monkeypatch.setattr(
        workspace_mod, "_sh",
        lambda *a, **k: pytest.fail("git ran under --dry-run"))
    c = merged_cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)
    # The merged branch really was taken (so _sh had its chance to run),
    # and the task stayed put — a dry run reports the teardown, never
    # performs it and never records it as performed.
    assert "[dry-run] remove worktree" in capsys.readouterr().out
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PR_OPEN


def test_spawned_cursor_still_sees_a_comment_from_the_spawn_second(
        tmp_path, monkeypatch):
    """End to end: spawn address-review, then post a comment stamped with
    the whole second the spawn happened in. The next poll must classify it
    as fresh feedback (a redundant round beats a lost comment)."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=waking(PR, "attention(feedback)"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    main.run_pass(c, deps(gh))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.ADDRESS_REVIEW and got.feedback_cursor

    # Derive the spawn second from the cursor the pass actually wrote.
    # _cursor_now() returns floor(now) - 1s, so the second a comment posted
    # during the spawn can carry is cursor + 1s.  Sampling the clock before
    # run_pass is racy: if the wall-clock second ticks over between the sample
    # and the _cursor_now() call, the derived stamp can be one second off.
    #
    # Non-tautology guard: assert spawn_second is not in the future.
    # If _cursor_now() stops subtracting, cursor = floor(now), making
    # spawn_second = floor(now) + 1s — a future second — and this assert fires.
    # (run_pass completes in <1 ms in tests; a 1-second gap is impossible.)
    cursor_dt = datetime.fromisoformat(got.feedback_cursor)
    spawn_second = cursor_dt + timedelta(seconds=1)
    assert spawn_second <= datetime.now(timezone.utc).replace(microsecond=0), (
        f"feedback_cursor {got.feedback_cursor!r} looks like floor(now), not "
        f"floor(now)-1s — _cursor_now() may have stopped subtracting"
    )
    stamp = spawn_second.strftime("%Y-%m-%dT%H:%M:%SZ")
    res = pr_poll.classify(
        payload(comments=[{"createdAt": stamp,
                           "author": {"login": "alice"}}]),
        got.feedback_cursor, "agent-bot")
    assert res.kind == "feedback"


def test_address_review_done_returns_task_to_pr_open(tmp_path, monkeypatch):
    """The return leg, at run_pass level: a task that STARTS the pass at
    address-review with a `done` signal must land back at pr-open with its
    PR number intact, and _poll_prs — which re-reads it later in the same
    pass, now against the freshly-advanced cursor — must not re-flag
    feedback for a comment older than that cursor or spawn a second round."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    cursor = "2026-07-30T09:30:00+00:00"
    wt = make_task(c, issue=42, stage=Stage.ADDRESS_REVIEW, slot=0,
                   pr_number=12, feedback_cursor=cursor)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "address-review", "status": "done",
        "note": "pushed review fixes", "artifact": ""}))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{
        "createdAt": "2026-07-30T09:00:00Z", "author": {"login": "alice"}}])
    sess = FakeSessions(alive=(42,))
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, sess, notifier=notif))

    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN
    assert got.pr_number == 12              # regex found nothing; not clobbered
    assert feedback_pending(got) is False    # comment predates the cursor
    assert got.feedback_cursor == cursor    # only a spawn moves it
    assert sess.spawned == []               # no second address-review round
    assert "pr_updated" in notif.sent


def test_dry_run_over_a_merged_pr_leaves_state_and_events_untouched(
        tmp_path, monkeypatch):
    """The reproduced production bug: a dry run over four merged PRs flipped
    them pr-open → done and appended four `merged` events, so the real pass
    that follows never performs the teardown (_poll_prs only sees PR_OPEN)."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    pr_open_task(c, pr_number=0)          # forces the pr_number backfill too
    gh = FakeGitHub()
    gh.branch_prs["agent/task-42"] = 12
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)

    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN
    assert got.done_at == ""
    assert got.pr_number == 0             # not even the benign backfill
    assert eventlog.read_tail(c.state_dir) == []


def test_dry_run_leaves_the_real_state_dir_alone_entirely(
        tmp_path, monkeypatch):
    """Belt-and-braces over the whole directory: no file under state_dir may
    change during a dry run, whatever the pass decides to do."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")

    def snapshot():
        root = Path(c.state_dir)
        return {p.relative_to(root): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()
                and p.name != "convergence.lock"}

    before = snapshot()
    main.run_pass(c, deps(gh), dry_run=True)
    assert snapshot() == before


def test_ci_conclusion_becomes_a_dispatcher_message(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=ci(4242))
    pr_watch._wake_ci(c, deps(gh=FakeGitHub(run_conclusion="failure")),
                  c.repositories[0])
    from dispatcher import messages
    queued = messages.undelivered(c.state_dir, "portfolio_eval", 42)
    assert len(queued) == 1
    assert "4242 concluded: failure" in queued[0].text
    assert queued[0].actor == "dispatcher"
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.status.run_id == 0


def test_finished_ci_run_wakes_the_task_through_the_table(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), capacity=0)
    make_task(c, issue=42, status=ci(991), slot=NO_SLOT)
    main.run_pass(c, deps(gh=FakeGitHub(run_conclusion="success")))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.status.cause is Cause.CI
    from dispatcher import messages
    assert ["991 concluded: success" in m.text
            for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [True]


def test_failed_ci_run_past_the_cap_parks_for_the_operator(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), capacity=0)
    make_task(c, issue=42, status=ci(991), slot=NO_SLOT, e2e_rounds=3)
    main.run_pass(c, deps(gh=FakeGitHub(run_conclusion="failure")))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.status.reason) == (Kind.PARKED_HUMAN, HumanReason.LOOP_CAP)
    assert t.e2e_rounds == 4
    assert [e["detail"] for e in _events(c, "round")] == ["e2e round 4/3: run 991 failure"]
    assert t.status.note == "e2e loop exceeded its cap of 3 rounds (run 991 failure)"


def test_ci_run_still_in_progress_leaves_the_task_parked(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), capacity=0)
    make_task(c, issue=42, status=ci(991), slot=NO_SLOT)
    main.run_pass(c, deps(gh=FakeGitHub(run_conclusion="")))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status == ci(991)
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []


def test_failed_e2e_runs_count_and_park_past_the_cap(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(e2e=1))
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "review"))
    make_task(c, stage=Stage.REVIEW, status=ci(7), slot=NO_SLOT)
    gh = FakeGitHub(run_conclusion="failure"); d = deps(gh)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    # Round 1 of 1: counted, last-round ping, woken AND resumed in the same pass.
    assert (t.status.kind, t.e2e_rounds) == (Kind.RUNNING, 1) and "last_round" in d.notifier.sent
    # Guard: last_round note must be suffix-free ("e2e round 1/1", not "e2e round 1/1: run 7 failure")
    last_round_note = next(ctx["note"] for tmpl, ctx in d.notifier.calls if tmpl == "last_round")
    assert last_round_note == "e2e round 1/1", f"last_round note regressed: {last_round_note!r}"
    assert d.sessions.resumed
    # Simulate the resumed session parking for CI again and failing again.
    save(c.state_dir, dc_replace(t, status=ci(8)))
    d = deps(FakeGitHub(run_conclusion="failure")); main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.e2e_rounds) == (Kind.PARKED_HUMAN, 2)
    assert "parked_question" in d.notifier.sent
    assert _events(c, "parked")[-1]["detail"] == "e2e loop exceeded its cap of 1 rounds (run 8 failure)"


def test_red_check_queues_address_review_with_its_reason(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier(); sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.ci_rounds, t.check_cursor) == (Stage.ADDRESS_REVIEW, 1, "2026-09-07T10:00:00Z")
    assert "pr_attention" in notif.sent
    assert "check-failed" in sess.spawned[-1][3]
    # Back at pr-open with the same red check: no second round.
    save(c.state_dir, dc_replace(t, stage=Stage.PR_OPEN, status=PR))
    d = deps(gh); main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PR_OPEN and not d.sessions.spawned


def test_conflict_queues_address_review_once_per_head(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="abc")
    sess = FakeSessions(); main.run_pass(c, deps(gh, sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.conflict_cursor) == (Stage.ADDRESS_REVIEW, "abc")
    assert "Reason for this round: conflict" in sess.spawned[-1][3]


def test_a_crashed_address_review_round_respawns_with_its_reason(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="abc")
    main.run_pass(c, deps(gh, FakeSessions()))
    t = load(c.state_dir, "portfolio_eval", 42)
    save(c.state_dir, dc_replace(t, status=waking(failed("session-crash"), "resume")))
    sess = FakeSessions(); main.run_pass(c, deps(gh, sess))
    [(_, stage, _, prompt, _)] = sess.spawned
    assert stage == "address-review" and "Reason for this round: conflict" in prompt


def test_ci_rounds_past_the_cap_park_the_pr_open_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(ci=1))
    pr_open_task(c, ci_rounds=1)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier(); sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind, feedback_pending(t)) == (
        Stage.PR_OPEN, Kind.PARKED_HUMAN, False)
    assert t.check_cursor == "2026-09-07T10:00:00Z"  # cursor persisted on exhaustion
    assert "parked_question" in notif.sent and not sess.spawned
    # Still polled for merge while parked.
    gh.pr_payloads[12] = payload(state="MERGED", merged_at="2026-09-07T12:00:00Z")
    patch_teardown(monkeypatch)
    main.run_pass(c, deps(gh))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.DONE


def test_human_feedback_resets_ci_rounds(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, ci_rounds=2)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{"createdAt": "2026-09-07T09:00:00Z",
                                           "author": {"login": "alice"}}])
    sess = FakeSessions(); main.run_pass(c, deps(gh, sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.ci_rounds == 0
    assert "Reason for this round: feedback" in sess.spawned[-1][3]


def test_ci_loop_exhausted_at_pr_open(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(ci=3))
    pr_open_task(c, status=PR, ci_rounds=3)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload(head="abc")
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier(); sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind, t.status.reason, t.ci_rounds) == (
        Stage.PR_OPEN, Kind.PARKED_HUMAN, HumanReason.LOOP_CAP, 4)
    assert t.check_cursor == "2026-09-07T10:00:00Z"
    assert "parked_question" in notif.sent and not sess.spawned
    # A later conflict is ignored: the task waits for the operator.
    gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="def")
    main.run_pass(c, deps(gh, sess))
    assert load(c.state_dir, "portfolio_eval", 42) == t


def test_one_red_check_seen_across_two_passes_charges_one_round(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 0)   # the round is not launched
    pr_open_task(c, status=PR)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status, t.ci_rounds) == (waking(PR, "attention(check-failed)"), 1)
    save(c.state_dir, dc_replace(t, status=PR))   # back on GitHub, same red check
    main.run_pass(c, deps(gh, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status, t.ci_rounds) == (PR, 1)
    assert notif.sent.count("pr_attention") == 1 and len(_events(c, "round")) == 1


def test_successful_ci_wake_preserves_all_counters(tmp_path):
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT, status=ci(99),
              review_rounds=1, gate_rounds=1, e2e_rounds=2, ci_rounds=1)
    pr_watch._wake_ci(c, deps(gh=FakeGitHub(run_conclusion="success")), c.repositories[0])
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (1, 1, 2, 1)


def test_pr_closed_failed_clears_operator_request(tmp_path, monkeypatch):
    """Slice 15a-ii: PR closed unmerged (_poll_prs ~L762) clears operator_request."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, spec_path="docs/specs/design.md")
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    main.run_pass(c, deps(gh, FakeSessions(alive=(42,))))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.FAILED
    assert operator_request(t) is None, (
        f"pr-closed FAILED must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"


@pytest.mark.parametrize("state,expected", [("MERGED", Kind.DONE), ("CLOSED", Kind.FAILED)])
def test_terminal_pr_does_not_require_ci_access(tmp_path, monkeypatch, state, expected):
    import dispatcher.github as github
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] != ["gh", "pr", "view"] or "statusCheckRollup" in args[-1]:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 403: CI denied")
        return json.dumps(payload(state=state, merged_at=(
            "2026-09-12T10:00:00Z" if state == "MERGED" else None)))

    monkeypatch.setattr(github, "_run", fake_run)
    client = github.GitHubClient()
    gh.pr_view = client.pr_view
    gh.ci_statuses = client.ci_statuses
    pr_watch._poll_prs(c, deps(gh), c.repositories[0])
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is expected
    assert len(calls) == 1  # terminal lifecycle never calls either CI endpoint


@pytest.mark.parametrize("feedback", [True, False])
def test_ci_denial_does_not_block_feedback_or_conflicts(tmp_path, monkeypatch, caplog, feedback):
    import dispatcher.github as github
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(
        mergeable="CONFLICTING",
        comments=[{"createdAt": "2026-09-12T11:00:00Z",
                   "author": {"login": "reviewer"}}] if feedback else [])

    def denied(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="HTTP 403: CI denied")

    monkeypatch.setattr(github, "_run", denied)
    gh.ci_statuses = github.GitHubClient().ci_statuses
    pr_watch._poll_prs(c, deps(gh), c.repositories[0])
    task = load(c.state_dir, "portfolio_eval", 42)
    assert feedback_pending(task)
    assert task.status.cause is (Cause.ATTENTION_FEEDBACK if feedback else Cause.ATTENTION_CONFLICT)
    if not feedback:
        assert "CI denied" in caplog.text
        assert task.check_cursor == ""


def test_parked_pr_does_not_poll_or_spend_ci_rounds(tmp_path):
    c = cfg(tmp_path)
    pr_open_task(c, status=human(), ci_rounds=2)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-12T10:00:00Z")]
    pr_watch._poll_prs(c, deps(gh), c.repositories[0])
    assert gh.ci_calls == []
    assert load(c.state_dir, "portfolio_eval", 42).ci_rounds == 2
