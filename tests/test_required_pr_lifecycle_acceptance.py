"""Acceptance tests for ticket 12 (project-onboarding): every required PR is
tracked on its own and the task finishes only when all of them merged.

Spec: specs/project-onboarding/spec.md S55, S56, S58, S61, S65, S66 (R15, R16,
R23, R24, R26). Ticket: .agent/tickets/12-required-pr-lifecycle.md. Seam:
dispatcher.main.run_pass(cfg, deps).

Interface these tests define (the ticket leaves its shape open):
- `state.RequiredPR(repo, number, state="open", head="", check_cursor="",
  feedback_cursor="", conflict_cursor="", ci_fix_attempts=0)` is one required
  PR of a task; `state` is "open" | "merged" | "closed" (closed without merge).
  `TaskState.required_prs` is the task's required set and
  `TaskState.required_set_revision` its revision. When the set is non-empty it
  is authoritative: the task is done only when every PR in it is "merged".
- `github.pr_view(target, number, repo=<owner/name>)` and
  `github.ci_statuses(target, head, branch, repo=<owner/name>)` read the PR of
  that repository; the poll never asks for a PR without naming its repository.
- A fix round for a failing check charges `ci_fix_attempts` of each PR it fixes,
  once, at most three per PR; a PR at three is never fixed again, the task parks
  (PARK_HUMAN, WHY_LOOP_CAP, `park_note` naming "<repo>#<number>") and stays
  unfinished. A required PR closed unmerged after another merged parks the task
  (PARK_HUMAN, WHY_DECISION) and sends the "partial_merge_guidance" notification
  once per incident (same required-set revision and outcomes).
"""
from dataclasses import replace

import pytest

import dispatcher.main as main
from dispatcher import state
from dispatcher.pr_poll import CIStatus
from dispatcher.state import (NO_SLOT, PARK_HUMAN, WHY_DECISION, WHY_LOOP_CAP, Stage,
                              load, load_all, save)

from tests.test_main import FakeGitHub, FakeNotifier, FakeSessions, deps, patch_usage, patch_workspace
from tests.test_project_reservations_acceptance import project, unfinished, world

FRONT, BACK = "example/shop-0", "example/shop-1"   # project shop's two repositories
SOLO = "example/solo-0"
ISSUE = 12
RED = "2026-10-19T10:00:00Z"
RED_LATER = "2026-10-19T11:00:00Z"
MERGED_AT = "2026-10-19T09:00:00Z"


def pr_payload(state="OPEN", head="deadbeef", comments=()):
    return {"state": state, "mergedAt": MERGED_AT if state == "MERGED" else None,
            "reviewDecision": "", "reviews": [], "comments": list(comments),
            "mergeable": "MERGEABLE", "headRefOid": head, "headRefName": "agent/task-12"}


class RepoGh(FakeGitHub):
    """GitHub that answers per repository-qualified PR."""

    def __init__(self):
        super().__init__()
        self.prs = {}          # (repo, number) -> payload, or an Exception to raise
        self.checks = {}       # repo -> [CIStatus]
        self.viewed = []       # (repo, number)

    def pr_view(self, target, number, repo=""):
        self.viewed.append((repo, number))
        got = self.prs.get((repo, number), pr_payload())
        if isinstance(got, Exception):
            raise got
        return got

    def ci_statuses(self, target, head, branch, repo=""):
        self.ci_calls.append((head, branch))
        return self.checks.get(repo, [])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "remove_workspace", lambda *a, **k: None)


def pr(repo, number, **kw):
    return state.RequiredPR(repo, number, **kw)


def shop_task(tmp_path, required, repos=2, key="shop", **kw):
    """One unfinished shop task at pr-open with the given required PRs."""
    c = world(tmp_path, [project(tmp_path, key, repos=repos)])
    (t,) = unfinished(c, key, 1, stage=Stage.PR_OPEN, park="", required_prs=tuple(required),
                      required_set_revision=1, **kw)
    return c, t


def task_of(c, t):
    return load(c.state_dir, t.target, t.issue, repo=t.repo)


def prs_of(c, t):
    return {(p.repo, p.number): p for p in task_of(c, t).required_prs}


def unfinished_count(c):
    return len([t for t in load_all(c.state_dir) if t.stage not in state.TERMINAL_STAGES])


def pass_(c, gh, sess=None, notif=None, passes=1):
    sess, notif = sess or FakeSessions(), notif or FakeNotifier()
    for _ in range(passes):
        main.run_pass(c, deps(gh, sess, notif))
    return sess, notif


def reopen(c, t):
    """The fix round ended: the task is back at pr-open for the next poll."""
    cur = task_of(c, t)
    if cur.stage is Stage.ADDRESS_REVIEW:
        save(c.state_dir, replace(cur, stage=Stage.PR_OPEN, slot=NO_SLOT))


def fix_rounds(sess):
    return len(sess.resumed) + len(sess.spawned)


# --- S55 -------------------------------------------------------------------

def test_s55_a_merged_backend_pr_leaves_the_task_unfinished_with_both_results(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 9), pr(BACK, 7)])
    gh = RepoGh()
    gh.prs[(BACK, 7)] = pr_payload("MERGED")
    gh.prs[(FRONT, 9)] = pr_payload("OPEN", head="f0f0f0f0")
    gh.checks[FRONT] = [CIStatus("failure", RED)]

    pass_(c, gh, passes=1)

    got = prs_of(c, t)
    assert got[(BACK, 7)].state == "merged"
    assert got[(FRONT, 9)].state == "open"
    assert got[(FRONT, 9)].check_cursor == RED          # the frontend failure is recorded
    assert task_of(c, t).stage not in state.TERMINAL_STAGES
    assert unfinished_count(c) == 1 and len(load_all(c.state_dir)) == 1


# --- S56 -------------------------------------------------------------------

def test_s56_the_last_required_pr_merging_completes_the_task_once(tmp_path):
    c, t = shop_task(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9)])
    gh = RepoGh()
    gh.prs[(BACK, 7)] = pr_payload("MERGED")
    gh.prs[(FRONT, 9)] = pr_payload("MERGED")
    notif = FakeNotifier()

    pass_(c, gh, notif=notif, passes=3)      # lifecycle polling repeats

    got = task_of(c, t)
    assert got.stage is Stage.DONE and got.done_at
    assert [(p.repo, p.number, p.state) for p in got.required_prs] == [
        (BACK, 7, "merged"), (FRONT, 9, "merged")]       # links to both PRs survive
    assert notif.sent.count("task_done") == 1
    assert len(load_all(c.state_dir)) == 1 and unfinished_count(c) == 0


# --- S58 -------------------------------------------------------------------

def test_s58_equal_pr_numbers_keep_separate_state_and_one_merge_is_not_done(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    gh = RepoGh()
    gh.prs[(FRONT, 7)] = pr_payload("MERGED", head="f0f0f0f0")
    gh.prs[(BACK, 7)] = pr_payload("OPEN", head="b4b4b4b4")

    pass_(c, gh, passes=2)

    got = prs_of(c, t)
    assert got[(FRONT, 7)].state == "merged"
    assert got[(BACK, 7)].state == "open"
    assert got[(BACK, 7)].head == "b4b4b4b4"
    assert {r for r, n in gh.viewed} == {FRONT, BACK}    # each read names its repository
    assert task_of(c, t).stage not in state.TERMINAL_STAGES


def test_one_pr_poll_cannot_advance_the_other_prs_cursors_or_head(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    gh = RepoGh()
    gh.prs[(FRONT, 7)] = pr_payload("OPEN", head="f0f0f0f0")
    gh.prs[(BACK, 7)] = pr_payload("OPEN", head="b4b4b4b4")
    gh.checks[FRONT] = [CIStatus("failure", RED)]

    pass_(c, gh)

    got = prs_of(c, t)
    assert (got[(FRONT, 7)].head, got[(FRONT, 7)].check_cursor) == ("f0f0f0f0", RED)
    assert (got[(BACK, 7)].head, got[(BACK, 7)].check_cursor) == ("b4b4b4b4", "")
    assert got[(BACK, 7)].ci_fix_attempts == 0


def test_an_unreadable_required_pr_is_not_treated_as_merged(tmp_path):
    c, t = shop_task(tmp_path, [pr(BACK, 7), pr(FRONT, 9)])
    gh = RepoGh()
    gh.prs[(BACK, 7)] = pr_payload("MERGED")
    gh.prs[(FRONT, 9)] = RuntimeError("gh unavailable")

    pass_(c, gh, passes=2)

    got = prs_of(c, t)
    assert got[(BACK, 7)].state == "merged"
    assert got[(FRONT, 9)].state == "open"
    assert task_of(c, t).stage not in state.TERMINAL_STAGES


# --- S61 -------------------------------------------------------------------

def test_s61_a_closed_required_pr_after_a_merge_parks_for_guidance_once(tmp_path):
    c, t = shop_task(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9)])
    gh = RepoGh()
    gh.prs[(BACK, 7)] = pr_payload("MERGED")
    gh.prs[(FRONT, 9)] = pr_payload("CLOSED")
    sess, notif = FakeSessions(), FakeNotifier()

    pass_(c, gh, sess, notif, passes=3)      # the same incident observed repeatedly

    got, prs = task_of(c, t), prs_of(c, t)
    assert (got.park, got.park_reason) == (PARK_HUMAN, WHY_DECISION)
    assert prs[(BACK, 7)].state == "merged" and prs[(FRONT, 9)].state == "closed"
    assert got.stage not in state.TERMINAL_STAGES and unfinished_count(c) == 1
    assert notif.sent.count("partial_merge_guidance") == 1
    assert "task_done" not in notif.sent and fix_rounds(sess) == 0
    assert len(got.required_prs) == 2        # neither reverted nor dropped


# --- S65 / S66 -------------------------------------------------------------

def test_s65_ci_fix_allowances_are_independent_per_pr(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=2), pr(BACK, 7)])
    gh = RepoGh()
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    gh.checks[BACK] = [CIStatus("failure", RED)]

    sess, _ = pass_(c, gh)

    got = prs_of(c, t)
    assert got[(FRONT, 7)].ci_fix_attempts == 3
    assert got[(BACK, 7)].ci_fix_attempts == 1
    assert fix_rounds(sess) == 1


def test_a_repeated_observation_of_the_same_failure_charges_one_attempt(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=2), pr(BACK, 7)])
    gh = RepoGh()
    gh.checks[FRONT] = [CIStatus("failure", RED)]

    sess, _ = pass_(c, gh)
    reopen(c, t)
    pass_(c, gh, sess, passes=2)

    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 3
    assert fix_rounds(sess) == 1


def test_s66_no_fourth_ci_fix_attempt_and_the_task_parks_unfinished(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=3), pr(BACK, 7)])
    gh = RepoGh()
    gh.prs[(FRONT, 7)] = pr_payload("OPEN", head="f0f0f0f0")
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    sess, notif = FakeSessions(), FakeNotifier()

    pass_(c, gh, sess, notif, passes=3)      # polling repeats
    # The task session resumes with another commit that still fails.
    gh.prs[(FRONT, 7)] = pr_payload("OPEN", head="0ddba11")
    gh.checks[FRONT] = [CIStatus("failure", RED_LATER)]
    pass_(c, gh, sess, notif, passes=2)

    got = task_of(c, t)
    assert fix_rounds(sess) == 0
    assert prs_of(c, t)[(FRONT, 7)].ci_fix_attempts == 3          # exhausted, visible
    assert (got.park, got.park_reason) == (PARK_HUMAN, WHY_LOOP_CAP)
    assert f"{FRONT}#7" in got.park_note
    assert got.stage not in state.TERMINAL_STAGES and unfinished_count(c) == 1
    assert prs_of(c, t)[(FRONT, 7)].state == "open"                # never merged by the pipeline


def test_a_single_repository_task_also_stops_at_three_ci_fix_attempts(tmp_path):
    c, t = shop_task(tmp_path, [pr(SOLO, 7, ci_fix_attempts=3)], repos=1, key="solo")
    gh = RepoGh()
    gh.checks[SOLO] = [CIStatus("failure", RED)]

    sess, _ = pass_(c, gh, passes=2)

    got = task_of(c, t)
    assert fix_rounds(sess) == 0 and prs_of(c, t)[(SOLO, 7)].ci_fix_attempts == 3
    assert (got.park, got.park_reason) == (PARK_HUMAN, WHY_LOOP_CAP)


# --- completion needs the whole required set -------------------------------

def test_an_all_merged_set_missing_a_bundle_repository_does_not_finish_the_task(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 9, state="merged")])
    state.save_bundle(c.state_dir, t.target, t.issue, state.TaskBundle(tuple(
        state.TaskRepository(r, str(tmp_path / r.split("/")[1]), t.branch)
        for r in (FRONT, BACK))), repo=t.repo)
    gh = RepoGh()                       # the backend PR does not exist yet
    notif = FakeNotifier()

    pass_(c, gh, notif=notif, passes=2)

    got = task_of(c, t)
    assert got.stage not in state.TERMINAL_STAGES and not got.done_at
    assert "task_done" not in notif.sent and unfinished_count(c) == 1


# --- single-repository task on the scalar PR path --------------------------

def test_a_natural_single_repository_task_stops_at_three_ci_fix_attempts(tmp_path):
    c = replace(world(tmp_path, [project(tmp_path, "solo", repos=1)]),
                loop_caps=state.LoopCaps(ci=10))      # the target allows more than three
    (t,) = unfinished(c, "solo", 1, stage=Stage.PR_OPEN, park="", pr_number=7, ci_rounds=3)
    gh = RepoGh()
    gh.checks[""] = [CIStatus("failure", RED)]
    assert not t.required_prs

    sess, _ = pass_(c, gh, passes=2)

    got = task_of(c, t)
    assert fix_rounds(sess) == 0
    assert (got.park, got.park_reason) == (PARK_HUMAN, WHY_LOOP_CAP)
    assert got.stage not in state.TERMINAL_STAGES


def test_a_set_revised_while_the_last_pr_merged_does_not_finish_the_task(tmp_path, monkeypatch):
    c, t = shop_task(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9)])
    gh = RepoGh()
    gh.prs[(BACK, 7)] = pr_payload("MERGED")
    gh.prs[(FRONT, 9)] = pr_payload("MERGED")
    notif = FakeNotifier()
    real_save = main.save

    def save_then_revise(state_dir, ts):
        real_save(state_dir, ts)
        if ts.stage is Stage.PR_OPEN and ts.required_set_revision == 1 and all(
                p.state == "merged" for p in ts.required_prs):
            # The operator changes the required set right after the poll read it.
            real_save(state_dir, replace(ts, required_set_revision=2))

    monkeypatch.setattr(main, "save", save_then_revise)

    pass_(c, gh, notif=notif)

    got = task_of(c, t)
    assert got.stage not in state.TERMINAL_STAGES and not got.done_at
    assert "task_done" not in notif.sent


def test_a_single_repository_task_with_spent_attempts_never_fixes_again_even_after_resume(tmp_path):
    c = replace(world(tmp_path, [project(tmp_path, "solo", repos=1)]),
                loop_caps=state.LoopCaps(ci=10))
    (t,) = unfinished(c, "solo", 1, stage=Stage.PR_OPEN, park="", pr_number=7,
                      ci_fix_attempts=3, ci_rounds=0)       # round counter reset, allowance spent
    gh = RepoGh()
    gh.checks[""] = [CIStatus("failure", RED)]

    sess, _ = pass_(c, gh)

    got = task_of(c, t)
    assert fix_rounds(sess) == 0 and got.ci_fix_attempts == 3
    assert (got.park, got.park_reason) == (PARK_HUMAN, WHY_LOOP_CAP)
    # The operator resumes the parked task and the check fails again.
    save(c.state_dir, replace(got, park="", park_reason="", ci_rounds=0, stage=Stage.PR_OPEN))
    gh.checks[""] = [CIStatus("failure", RED_LATER)]

    pass_(c, gh, sess, passes=2)

    got = task_of(c, t)
    assert fix_rounds(sess) == 0 and got.ci_fix_attempts == 3
    assert (got.park, got.park_reason) == (PARK_HUMAN, WHY_LOOP_CAP)


def review_comment(ts):
    return {"createdAt": ts, "author": {"login": "reviewer"}, "body": "please fix"}


def test_review_feedback_is_tracked_on_the_commented_pr_only_and_not_requeued(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    c = replace(c, capacity=0)                    # the round stays queued across passes
    gh = RepoGh()
    gh.prs[(FRONT, 7)] = pr_payload("OPEN", head="f0f0f0f0", comments=[review_comment(RED)])
    gh.prs[(BACK, 7)] = pr_payload("OPEN", head="b4b4b4b4")

    pass_(c, gh, passes=3)

    got = prs_of(c, t)
    assert got[(FRONT, 7)].feedback_cursor == RED
    assert got[(FRONT, 7)].feedback_rounds == 1               # observed once, counted once
    assert got[(BACK, 7)].feedback_cursor == "" and got[(BACK, 7)].feedback_rounds == 0
    assert task_of(c, t).feedback_pending and task_of(c, t).feedback_pr == f"{FRONT}#7"


def test_a_conflict_cursor_advances_only_on_the_conflicting_pr(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7), pr(BACK, 7)])
    c = replace(c, capacity=0)
    gh = RepoGh()
    gh.prs[(FRONT, 7)] = {**pr_payload("OPEN", head="f0f0f0f0"), "mergeable": "CONFLICTING"}
    gh.prs[(BACK, 7)] = pr_payload("OPEN", head="b4b4b4b4")

    pass_(c, gh, passes=2)

    got = prs_of(c, t)
    assert got[(FRONT, 7)].conflict_cursor and not got[(BACK, 7)].conflict_cursor
    assert not got[(FRONT, 7)].feedback_cursor and not got[(BACK, 7)].feedback_cursor
    assert task_of(c, t).repair_prs == (f"{FRONT}#7",)


class DispatcherExit(BaseException):
    pass


def test_an_interrupted_repair_launch_is_charged_once_and_no_fourth_attempt_follows(tmp_path):
    c, t = shop_task(tmp_path, [pr(FRONT, 7, ci_fix_attempts=2), pr(BACK, 7, ci_fix_attempts=2)])
    gh = RepoGh()
    gh.checks[FRONT] = [CIStatus("failure", RED)]
    gh.checks[BACK] = [CIStatus("failure", RED)]
    sess = FakeSessions()
    spawn = sess.spawn_stage

    def spawn_then_exit(*args, **kw):
        spawn(*args, **kw)
        raise DispatcherExit()          # the dispatcher dies right after the session started

    sess.spawn_stage = spawn_then_exit
    with pytest.raises(DispatcherExit):
        pass_(c, gh, sess=sess)
    sess.spawn_stage = spawn

    pass_(c, gh, sess, passes=3)        # restarted dispatcher keeps polling

    got = prs_of(c, t)
    assert (got[(FRONT, 7)].ci_fix_attempts, got[(BACK, 7)].ci_fix_attempts) == (3, 3)
    assert len(sess.spawned) == 1       # one launch for both PRs, never a fourth attempt
