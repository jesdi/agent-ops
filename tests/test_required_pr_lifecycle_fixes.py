"""Ticket 12 review fixes: required-set discovery, charging CI fixes at launch,
partial-merge incidents, unreadable and reopened PRs, review-feedback cap,
and a required PR's visible check status."""
from dataclasses import replace
from datetime import datetime, timezone

from dispatcher import state
from dispatcher.pr_poll import CIStatus
from dispatcher.state import (PARK_HUMAN, WHY_DECISION, WHY_LOOP_CAP, WHY_OPERATOR, Stage,
                              TaskBundle, TaskRepository, save, save_bundle)

from tests.test_main import FakeNotifier, FakeSessions
from tests.test_required_pr_lifecycle_acceptance import (
    BACK, FRONT, RED, RepoGh, _env, pass_, pr, pr_payload, prs_of, reopen, review_comment,
    shop_task, task_of)  # noqa: F401
from web.read_model import task_detail


def park(c, t, reason):
    save(c.state_dir, replace(task_of(c, t), park=PARK_HUMAN, park_reason=reason))


def bundled_task(tmp_path, **kw):
    c, t = shop_task(tmp_path, [], **kw)
    save_bundle(c.state_dir, t.target, t.issue, TaskBundle(
        repositories=tuple(TaskRepository(r, t.worktree, t.branch) for r in (FRONT, BACK))),
        repo=t.repo)
    return c, t


class BranchGh(RepoGh):
    def __init__(self, found):
        super().__init__()
        self.found = found

    def pr_number_for_branch(self, target, branch):
        return self.found.get(target.repo, 0)


def test_a_bundled_task_builds_its_required_set_and_one_merge_is_not_done(tmp_path):
    c, t = bundled_task(tmp_path)
    gh = BranchGh({FRONT: 9, BACK: 7})
    gh.prs[(BACK, 7)] = pr_payload("MERGED")

    pass_(c, gh)

    got = task_of(c, t)
    assert sorted((p.repo, p.number, p.state) for p in got.required_prs) == [
        (FRONT, 9, "open"), (BACK, 7, "merged")]
    assert got.required_set_revision == 1 and got.stage is Stage.PR_OPEN


def test_the_set_waits_for_every_repository_to_have_a_pr(tmp_path):
    c, t = bundled_task(tmp_path)

    pass_(c, BranchGh({BACK: 7}))

    assert task_of(c, t).required_prs == ()


def test_a_pr_lookup_failure_leaves_the_set_unbuilt(tmp_path):
    c, t = bundled_task(tmp_path)
    gh = BranchGh({})
    gh.pr_number_for_branch = lambda *a: (_ for _ in ()).throw(OSError("gh down"))

    pass_(c, gh)

    assert task_of(c, t).required_prs == ()


def test_a_widened_scope_blocks_completion_until_its_pr_joins_the_set(tmp_path):
    c, t = bundled_task(tmp_path)
    save(c.state_dir, replace(task_of(c, t), required_prs=(pr(BACK, 7),)))
    gh = BranchGh({})
    gh.prs[(BACK, 7)] = pr_payload("MERGED")

    pass_(c, gh)

    assert task_of(c, t).stage is Stage.PR_OPEN          # FRONT has no PR yet


def test_ci_attempts_are_charged_at_launch_not_at_observation(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    c = replace(c, capacity=0)
    gh = RepoGh()
    gh.checks[FRONT] = [CIStatus("failure", RED)]

    sess, _ = pass_(c, gh)

    assert not sess.spawned and not sess.resumed
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 0
    assert task_of(c, t).repair_prs == (f"{FRONT}#7",)


def test_the_repair_round_names_the_pr_it_fixes(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 9), pr(BACK, 7)])
    gh = RepoGh()
    gh.checks[BACK] = [CIStatus("failure", RED)]

    sess, _ = pass_(c, gh)

    (_, _, _, prompt, _), = sess.spawned
    assert f"--repo {BACK}" in prompt and "pull request #7" in prompt


def test_review_feedback_stops_at_the_cap_per_pr(tmp_path):
    rounds = state.LoopCaps().review
    c, t = shop_task(tmp_path, [pr(FRONT, 7, feedback_rounds=rounds)])
    gh = RepoGh()
    gh.prs[(FRONT, 7)] = pr_payload("OPEN", comments=[
        {"createdAt": "2026-10-19T10:00:00Z", "author": {"login": "reviewer"}, "body": "fix"}])

    pass_(c, gh)

    got = task_of(c, t)
    assert (got.park, got.park_reason) == (PARK_HUMAN, WHY_LOOP_CAP)
    assert not got.feedback_pending and f"{FRONT}#7" in got.park_note


def test_review_feedback_within_the_cap_queues_a_round_and_counts_it(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    gh = RepoGh()
    gh.prs[(FRONT, 7)] = pr_payload("OPEN", comments=[
        {"createdAt": "2026-10-19T10:00:00Z", "author": {"login": "reviewer"}, "body": "fix"}])

    pass_(c, gh)

    assert prs_of(c, t)[(FRONT, 7)].feedback_rounds == 1


def test_an_unrelated_park_does_not_hide_the_partial_merge_guidance(tmp_path):
    c, t = shop_task(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9)])
    park(c, t, WHY_LOOP_CAP)
    gh = RepoGh()
    gh.prs[(BACK, 7)] = pr_payload("MERGED")
    gh.prs[(FRONT, 9)] = pr_payload("CLOSED")
    notif = FakeNotifier()

    pass_(c, gh, notif=notif, passes=2)

    assert notif.sent.count("partial_merge_guidance") == 1
    assert task_of(c, t).park_reason == WHY_DECISION


def test_an_unreadable_pr_defers_the_failure_of_a_task_with_a_closed_pr(tmp_path):
    c, t = shop_task(tmp_path, [pr(BACK, 7), pr(FRONT, 9)])
    gh = RepoGh()
    gh.prs[(BACK, 7)] = RuntimeError("gh unavailable")
    gh.prs[(FRONT, 9)] = pr_payload("CLOSED")

    pass_(c, gh)

    assert task_of(c, t).stage is Stage.PR_OPEN


def test_a_required_pr_closed_with_nothing_merged_fails_the_task(tmp_path):
    c, t = shop_task(tmp_path, [pr(BACK, 7), pr(FRONT, 9)])
    gh = RepoGh()
    gh.prs[(FRONT, 9)] = pr_payload("CLOSED")
    notif = FakeNotifier()

    pass_(c, gh, notif=notif)

    assert task_of(c, t).stage is Stage.FAILED and "pr_closed" in notif.sent


def test_a_reopened_pr_recovers_and_the_task_can_complete(tmp_path):
    c, t = shop_task(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9, state="closed")])
    park(c, t, WHY_OPERATOR)
    gh = RepoGh()
    gh.prs[(FRONT, 9)] = pr_payload("MERGED")

    pass_(c, gh)

    assert task_of(c, t).stage is Stage.DONE


def test_scalar_ci_waits_for_capacity_before_spending_attempt(tmp_path):
    from tests.test_main import FakeGitHub
    c, t = shop_task(tmp_path, [], repos=1, pr_number=7)
    c = replace(c, capacity=0)
    gh = FakeGitHub()
    gh.ci = [CIStatus("failure", RED)]
    sess, _ = pass_(c, gh)
    assert not sess.spawned
    assert task_of(c, t).ci_fix_attempts == 0


def test_widening_transfers_scalar_allowance_and_cursors(tmp_path):
    c, t = bundled_task(tmp_path, pr_number=7, ci_fix_attempts=3,
                        check_cursor="old-check", conflict_cursor="old-head",
                        feedback_cursor="old-feedback")
    pass_(c, BranchGh({FRONT: 7, BACK: 9}))
    got = prs_of(c, t)[(FRONT, 7)]
    assert got.ci_fix_attempts == 3
    assert (got.check_cursor, got.conflict_cursor, got.feedback_cursor) == (
        "old-check", "old-head", "old-feedback")


class DispatcherExit(BaseException):
    pass


def test_repair_charge_survives_exit_immediately_after_external_spawn(tmp_path):
    import pytest
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=2)])
    gh = RepoGh()
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    sess = FakeSessions()
    spawn = sess.spawn_stage
    def spawn_then_exit(*args, **kw):
        spawn(*args, **kw)
        raise DispatcherExit()
    sess.spawn_stage = spawn_then_exit
    with pytest.raises(DispatcherExit):
        pass_(c, gh, sess=sess)
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 3
    pass_(c, gh, passes=2)
    assert task_of(c, t).park == PARK_HUMAN
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 3


def test_partial_guidance_survives_exit_after_delivery_without_resending(tmp_path):
    import pytest
    c, t = shop_task(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9)])
    gh = RepoGh()
    gh.prs[(FRONT, 9)] = pr_payload("CLOSED")
    notif = FakeNotifier()
    send = notif.send
    def send_then_exit(template, **ctx):
        result = send(template, **ctx)
        if template == "partial_merge_guidance":
            raise DispatcherExit()
        return result
    notif.send = send_then_exit
    with pytest.raises(DispatcherExit):
        pass_(c, gh, notif=notif)
    notif.send = send
    pass_(c, gh, notif=notif, passes=2)
    assert notif.sent.count("partial_merge_guidance") == 1
    assert task_of(c, t).park_reason == WHY_DECISION
    assert task_of(c, t).partial_delivery == "unknown"


def required_command(c, t, action, number, replacement=0):
    from dispatcher.intents import write_intent
    write_intent(c.state_dir, action, t.target, t.issue,
                 {"pr_repo": FRONT, "number": number, "replacement": replacement,
                  "revision": task_of(c, t).required_set_revision},
                 "operator", 123, repo=t.repo)


def test_operator_replaces_closed_pr_in_same_repo_and_revision_advances(tmp_path):
    c, t = shop_task(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9, state="closed")])
    park(c, t, WHY_DECISION)
    required_command(c, t, "replace-pr", 9, 10)
    pass_(c, RepoGh())
    got = task_of(c, t)
    assert [(p.repo, p.number) for p in got.required_prs] == [(BACK, 7), (FRONT, 10)]
    assert got.required_set_revision == 2


def test_explicit_drop_is_not_rediscovered_from_bundle(tmp_path):
    c, t = bundled_task(tmp_path)
    save(c.state_dir, replace(task_of(c, t), required_prs=(
        pr(BACK, 7, state="merged"), pr(FRONT, 9, state="closed"))))
    required_command(c, t, "drop-pr", 9)
    pass_(c, BranchGh({FRONT: 9, BACK: 7}), passes=2)
    assert task_of(c, t).stage is Stage.DONE
    assert [(p.repo, p.number) for p in task_of(c, t).required_prs] == [(BACK, 7)]


def test_scalar_ci_charges_only_the_launched_round_and_never_a_fourth(tmp_path):
    from tests.test_main import FakeGitHub
    c, t = shop_task(tmp_path, [], repos=1, pr_number=7, ci_fix_attempts=2)
    gh = FakeGitHub()
    gh.ci = [CIStatus("failure", RED)]
    sess, _ = pass_(c, gh)
    assert len(sess.spawned) == 1
    assert task_of(c, t).ci_fix_attempts == 3
    save(c.state_dir, replace(task_of(c, t), stage=Stage.PR_OPEN))
    gh.ci = [CIStatus("failure", "2026-10-19T11:00:00Z")]
    pass_(c, gh, sess=sess)
    assert len(sess.spawned) == 1
    assert task_of(c, t).park_reason == WHY_LOOP_CAP


def test_interrupted_launch_adopts_live_session_without_charging_again(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=3)])
    save(c.state_dir, replace(task_of(c, t), stage=Stage.ADDRESS_REVIEW,
                              repair_launch_reserved=True))
    sess, _ = pass_(c, RepoGh(), sess=FakeSessions(alive={t.issue}))
    assert not sess.spawned
    assert not task_of(c, t).repair_launch_reserved
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 3


def test_guidance_save_failure_cannot_send_before_incident_is_durable(tmp_path, monkeypatch):
    import pytest
    import dispatcher.main as main
    c, t = shop_task(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9)])
    gh = RepoGh()
    gh.prs[(FRONT, 9)] = pr_payload("CLOSED")
    real_save = main.save
    def fail_reservation(directory, task):
        if task.partial_delivery == "sending":
            raise DispatcherExit()
        real_save(directory, task)
    monkeypatch.setattr(main, "save", fail_reservation)
    notif = FakeNotifier()
    with pytest.raises(DispatcherExit):
        pass_(c, gh, notif=notif)
    assert "partial_merge_guidance" not in notif.sent
    monkeypatch.setattr(main, "save", real_save)
    pass_(c, gh, notif=notif, passes=2)
    assert notif.sent.count("partial_merge_guidance") == 1


def test_rejected_commands_leave_the_required_set_and_revision_unchanged(tmp_path):
    from dispatcher.intents import write_intent
    c, t = shop_task(tmp_path, [pr(FRONT, 9, state="closed"), pr(BACK, 7)])
    park(c, t, WHY_DECISION)
    payloads = [
        {"pr_repo": FRONT, "number": 9, "revision": 0, "replacement": 10},
        {"pr_repo": "other/repo", "number": 9, "revision": 1, "replacement": 10},
        {"pr_repo": BACK, "number": 7, "revision": 1, "replacement": 10},
        {"pr_repo": FRONT, "number": 9, "revision": 1, "replacement": 9},
        {"pr_repo": FRONT, "number": 9, "revision": 1, "replacement": 10},
    ]
    gh = RepoGh()
    gh.prs[(FRONT, 9)] = pr_payload("CLOSED")
    gh.prs[(FRONT, 10)] = pr_payload("CLOSED")
    for index, payload in enumerate(payloads):
        write_intent(c.state_dir, "replace-pr", t.target, t.issue, payload,
                     "operator", index, repo=t.repo)
        pass_(c, gh)
        assert [(p.repo, p.number) for p in task_of(c, t).required_prs] == [(FRONT, 9), (BACK, 7)]
        assert task_of(c, t).required_set_revision == 1


def test_drop_last_required_pr_is_refused(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 9)])
    required_command(c, t, "drop-pr", 9)
    pass_(c, RepoGh())
    assert task_of(c, t).required_prs[0].number == 9
    assert task_of(c, t).required_set_revision == 1


def test_terminal_command_queues_revision_checked_intent(tmp_path, monkeypatch, capsys):
    from dispatcher import intents, required_pr_commands
    c, t = shop_task(tmp_path, [pr(FRONT, 9)])
    monkeypatch.setattr("sys.argv", ["required_pr_commands", "--state-dir", c.state_dir,
        "--target", t.target, "--repo", t.repo, "--issue", str(t.issue),
        "--revision", "1", "replace-pr", f"{FRONT}#9", "10"])
    required_pr_commands.main()
    intent, = intents.list_intents(c.state_dir)
    assert intent.action == "replace-pr" and intent.repo == t.repo
    assert intent.payload == {"pr_repo": FRONT, "number": 9, "replacement": 10, "revision": 1}
    assert "Queued replace-pr" in capsys.readouterr().out


def test_failed_ci_resume_never_starts_a_fourth_repair(tmp_path):
    from dispatcher.state import PARK_CI
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=2), pr(BACK, 7)])
    gh = RepoGh()
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    sess, _ = pass_(c, gh)
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 3
    save(c.state_dir, replace(task_of(c, t), park=PARK_CI, ci_run_id=123))
    gh.run_conclusion = "failure"

    pass_(c, gh, sess=sess, passes=2)

    assert len(sess.spawned) + len(sess.resumed) == 1
    assert task_of(c, t).park_reason == WHY_LOOP_CAP
    assert f"{FRONT}#7" in task_of(c, t).park_note
    assert prs_of(c, t)[(BACK, 7)].ci_fix_attempts == 0


def test_failed_ci_resume_waits_for_capacity_then_charges_once(tmp_path):
    from dispatcher.state import PARK_CI
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=1), pr(BACK, 7)])
    state.write_session(c.state_dir, t.target, t.issue,
                        state.SessionRecord("repair-session", "address-review"), repo=t.repo)
    save(c.state_dir, replace(t, stage=Stage.ADDRESS_REVIEW, park=PARK_CI,
        ci_run_id=123, attention="check-failed", repair_prs=(f"{FRONT}#7",)))
    gh = RepoGh()
    gh.run_conclusion = "failure"
    sess, _ = pass_(replace(c, capacity=0), gh)
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 1
    pass_(c, gh, sess=sess)
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 2
    assert prs_of(c, t)[(BACK, 7)].ci_fix_attempts == 0
    assert len(sess.resumed) == 1 and not sess.spawned
    assert not task_of(c, t).repair_launch_reserved


def test_pending_repair_is_discarded_when_its_pr_merges(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    gh = RepoGh()
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    pass_(replace(c, capacity=0), gh)
    assert task_of(c, t).feedback_pending
    gh.prs[(FRONT, 7)] = pr_payload("MERGED")

    sess, _ = pass_(c, gh)

    assert not sess.spawned and not sess.resumed
    assert not task_of(c, t).feedback_pending
    assert task_of(c, t).repair_prs == ()
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 0
    assert task_of(c, t).stage is Stage.PR_OPEN


def test_scalar_failed_ci_resume_uses_persistent_limit(tmp_path):
    from dispatcher.state import PARK_CI
    from tests.test_main import FakeGitHub
    c, t = shop_task(tmp_path, [], repos=1, pr_number=7, ci_fix_attempts=2)
    save(c.state_dir, replace(t, stage=Stage.ADDRESS_REVIEW, park=PARK_CI,
                              ci_run_id=123, attention="feedback"))
    gh = FakeGitHub(run_conclusion="failure")
    sess, _ = pass_(c, gh)
    assert task_of(c, t).ci_fix_attempts == 3
    save(c.state_dir, replace(task_of(c, t), park=PARK_CI, ci_run_id=124, ci_rounds=0))
    pass_(c, gh, sess=sess)
    assert len(sess.spawned) + len(sess.resumed) == 1
    assert task_of(c, t).park_reason == WHY_LOOP_CAP


def test_successful_verification_does_not_charge_a_repair(tmp_path):
    from dispatcher.state import PARK_CI
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=3)])
    save(c.state_dir, replace(t, stage=Stage.ADDRESS_REVIEW, park=PARK_CI,
        ci_run_id=123, attention="check-failed", repair_prs=(f"{FRONT}#7",)))
    gh = RepoGh()
    gh.run_conclusion = "success"
    sess, _ = pass_(c, gh)
    assert len(sess.spawned) + len(sess.resumed) == 1
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 3


def test_failed_review_verification_charges_its_repository(tmp_path):
    from dispatcher.state import PARK_CI
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    save(c.state_dir, replace(t, stage=Stage.ADDRESS_REVIEW, park=PARK_CI,
        ci_run_id=123, attention="feedback", feedback_pr=f"{BACK}#7"))
    gh = RepoGh()
    gh.run_conclusion = "failure"
    pass_(c, gh)
    assert prs_of(c, t)[(BACK, 7)].ci_fix_attempts == 1
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 0


def test_failed_ci_resume_reservation_survives_interruption(tmp_path):
    import pytest
    from dispatcher.state import PARK_CI
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=2)])
    save(c.state_dir, replace(t, stage=Stage.ADDRESS_REVIEW, park=PARK_CI,
        ci_run_id=123, attention="check-failed", repair_prs=(f"{FRONT}#7",)))
    gh = RepoGh()
    gh.run_conclusion = "failure"
    sess = FakeSessions()
    spawn = sess.spawn_stage
    def spawn_then_exit(*args, **kw):
        spawn(*args, **kw)
        raise DispatcherExit()
    sess.spawn_stage = spawn_then_exit
    with pytest.raises(DispatcherExit):
        pass_(c, gh, sess=sess)
    assert task_of(c, t).repair_launch_reserved
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 3
    recovered, _ = pass_(c, gh, passes=2)
    assert not recovered.spawned and not recovered.resumed
    assert task_of(c, t).park_reason == WHY_LOOP_CAP


def test_pending_round_keeps_only_the_remaining_open_pr(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    gh = RepoGh()
    gh.checks = {repo: [CIStatus("failure", RED)] for repo in (FRONT, BACK)}
    pass_(replace(c, capacity=0), gh)
    gh.prs[(FRONT, 7)] = pr_payload("MERGED")
    sess, _ = pass_(c, gh)
    assert len(sess.spawned) == 1
    assert task_of(c, t).repair_prs == (f"{BACK}#7",)
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 0
    assert prs_of(c, t)[(BACK, 7)].ci_fix_attempts == 1
    assert f"--repo {BACK}" in sess.spawned[0][3]


# --- the visible check status (findings 1, 2) --------------------------------

GREEN = "2026-10-19T12:00:00Z"


def shown(c, t):
    """The task view's required PRs as (repository-qualified PR, state, checks)."""
    detail = task_detail(task_of(c, t), model="m", pane_tail="", session_alive=False,
                         events=[], now=datetime.now(timezone.utc))
    return {f"{p.repo}#{p.number}": (p.state, p.checks) for p in detail.required_prs}


def test_s55_the_frontend_failure_stays_visible_across_polls_while_repair_waits(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 9), pr(BACK, 7)])
    c = replace(c, capacity=0)                    # the repair waits for a place
    gh = RepoGh()
    gh.prs[(BACK, 7)] = pr_payload("MERGED")
    gh.prs[(FRONT, 9)] = pr_payload("OPEN", head="f0f0f0f0")
    gh.checks[FRONT] = [CIStatus("failure", RED)]

    for _ in range(3):
        pass_(c, gh)
        assert shown(c, t)[f"{FRONT}#9"] == ("open", "check-failed")
        assert shown(c, t)[f"{BACK}#7"][0] == "merged"
    assert task_of(c, t).feedback_pending


def test_the_failure_stays_visible_while_feedback_is_pending(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 9), pr(BACK, 7)])
    c = replace(c, capacity=0)
    gh = RepoGh()
    gh.prs[(FRONT, 9)] = pr_payload("OPEN", head="f0f0f0f0")
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    pass_(c, gh)

    gh.prs[(FRONT, 9)] = pr_payload("OPEN", head="f0f0f0f0", comments=[review_comment(GREEN)])
    pass_(c, gh, passes=2)

    assert shown(c, t)[f"{FRONT}#9"] == ("open", "check-failed")


def test_a_later_passing_read_replaces_the_failure(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 9), pr(BACK, 7)])
    gh = RepoGh()
    gh.prs[(FRONT, 9)] = pr_payload("OPEN", head="f0f0f0f0")
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    pass_(c, gh)
    assert shown(c, t)[f"{FRONT}#9"] == ("open", "check-failed")

    reopen(c, t)
    gh.prs[(FRONT, 9)] = pr_payload("OPEN", head="f1f1f1f1")
    gh.checks[FRONT] = [CIStatus("success", GREEN)]
    pass_(c, gh)

    assert shown(c, t)[f"{FRONT}#9"] == ("open", "passing")


def test_s54_the_view_shows_each_required_pr_with_its_own_check_status(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    c = replace(c, capacity=0)
    gh = RepoGh()
    gh.prs[(FRONT, 7)] = pr_payload("OPEN", head="f0f0f0f0")
    gh.prs[(BACK, 7)] = pr_payload("OPEN", head="b4b4b4b4")
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    gh.checks[BACK] = [CIStatus("success", RED), CIStatus("", "")]

    pass_(c, gh, passes=2)

    assert shown(c, t) == {f"{FRONT}#7": ("open", "check-failed"),
                           f"{BACK}#7": ("open", "pending")}
