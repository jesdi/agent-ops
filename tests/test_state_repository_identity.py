"""Acceptance tests for ticket 03 (project-onboarding): durable task state and
its sidecars carry a repository-qualified identity.

Spec: specs/project-onboarding/spec.md requirement 24 (and 21 for legacy
records). Seam: STATE -- dispatcher.state save/load/load_all/archive, plus the
per-task sidecars that share the task key.

Interface these tests define (the ticket leaves its shape open):
- TaskState gains `repo: str = ""`, the canonical "owner/name" of the origin
  repository. "" marks a legacy record whose repository is not proven.
- Every per-task operation of state, messages, intents, execution_overrides,
  failures and task_artifacts takes a keyword-only `repo: str = ""` next to
  (target, issue): state.load/read_session/write_session/mark_waiting/
  has_waiting/mark_background/read_background/clear_turn_markers/
  archive_root/load_archived, messages.append/all_messages/undelivered/
  mark_delivered, intents.write_intent (and Intent.repo),
  execution_overrides.save/load, failures.write_quarantine/check_quarantine,
  task_artifacts.content_path. FailureReport gains `repo`.
- state.migrate_legacy(state_dir, proven) takes {target: "owner/name"}, the
  proven single-repository mapping per target. It moves task, archive, message
  and session records of those targets to the qualified identity and leaves
  every other legacy record untouched and unresolved (repo == "").
"""
import os
from dataclasses import replace
from pathlib import Path

import pytest

from dispatcher import (answers, execution_overrides, failures, intents,
                        messages, state, task_artifacts)
from dispatcher.state import (AnswersRequest, SessionRecord, Stage, TaskState,
                              load, load_all, save)

TARGET = "shop"
FRONT, BACK = "example/frontend", "example/backend"
ISSUE = 12


def task(repo, stage=Stage.IMPLEMENT, target=TARGET, issue=ISSUE, **kw):
    fields = dict(slot=0, worktree=f"/w/{repo}", branch=f"b-{repo.replace('/', '-')}",
                  title=f"title of {repo}", updated_at="2026-10-16T00:00:00+00:00")
    return TaskState(issue=issue, target=target, stage=stage, repo=repo,
                     **{**fields, **kw})


def test_same_number_tasks_keep_independent_task_and_session_state(tmp_path):
    save(tmp_path, task(FRONT, Stage.IMPLEMENT, ci_rounds=1))
    save(tmp_path, task(BACK, Stage.PLAN, ci_rounds=2))

    front, back = (load(tmp_path, TARGET, ISSUE, repo=r) for r in (FRONT, BACK))
    assert (front.repo, front.stage, front.ci_rounds) == (FRONT, Stage.IMPLEMENT, 1)
    assert (back.repo, back.stage, back.ci_rounds) == (BACK, Stage.PLAN, 2)
    assert {t.repo for t in load_all(tmp_path)} == {FRONT, BACK}

    state.write_session(tmp_path, TARGET, ISSUE, SessionRecord("sid-f", "implement"),
                        repo=FRONT)
    state.mark_waiting(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert state.read_session(tmp_path, TARGET, ISSUE, repo=BACK) is None
    assert state.read_session(tmp_path, TARGET, ISSUE, repo=FRONT).session_id == "sid-f"
    assert state.has_waiting(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert not state.has_waiting(tmp_path, TARGET, ISSUE, repo=BACK)

    state.write_session(tmp_path, TARGET, ISSUE, SessionRecord("sid-b", "plan"), repo=BACK)
    state.mark_waiting(tmp_path, TARGET, ISSUE, repo=BACK)
    state.clear_turn_markers(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert not state.has_waiting(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert state.has_waiting(tmp_path, TARGET, ISSUE, repo=BACK)
    state.clear_session(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert state.read_session(tmp_path, TARGET, ISSUE, repo=BACK).session_id == "sid-b"


def test_same_number_tasks_keep_independent_messages_and_intents(tmp_path):
    m = messages.append(tmp_path, TARGET, ISSUE, "for front", "operator", repo=FRONT)
    messages.append(tmp_path, TARGET, ISSUE, "for back", "operator", repo=BACK)
    messages.mark_delivered(tmp_path, TARGET, ISSUE, [m.id], repo=FRONT)

    assert messages.undelivered(tmp_path, TARGET, ISSUE, repo=FRONT) == []
    assert [x.text for x in messages.undelivered(tmp_path, TARGET, ISSUE, repo=BACK)] == ["for back"]

    # Same millisecond, target, issue and action: only the repository tells them apart.
    a = intents.write_intent(tmp_path, "answers", TARGET, ISSUE, {}, "op", 1, repo=FRONT)
    b = intents.write_intent(tmp_path, "answers", TARGET, ISSUE, {}, "op", 1, repo=BACK)
    assert a != b and a.exists() and b.exists()
    assert sorted(i.repo for i in intents.list_intents(tmp_path)) == [BACK, FRONT]


def test_same_number_tasks_keep_independent_background_and_wake_markers(tmp_path):
    state.mark_background(tmp_path, TARGET, ISSUE, [{"id": "a"}], now=10.0, repo=FRONT)
    assert state.read_background(tmp_path, TARGET, ISSUE, repo=BACK) is None
    state.mark_background(tmp_path, TARGET, ISSUE, [{"id": "b"}], now=20.0, repo=BACK)
    assert state.read_background(tmp_path, TARGET, ISSUE, repo=FRONT).tasks == ("a",)
    assert state.read_background(tmp_path, TARGET, ISSUE, repo=BACK).tasks == ("b",)

    # A turn end of one task replaces only its own marker.
    state.mark_waiting(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert state.read_background(tmp_path, TARGET, ISSUE, repo=FRONT) is None
    assert state.read_background(tmp_path, TARGET, ISSUE, repo=BACK).tasks == ("b",)
    state.mark_waiting(tmp_path, TARGET, ISSUE, repo=BACK)
    state.mark_background(tmp_path, TARGET, ISSUE, [{"id": "a"}], now=30.0, repo=FRONT)
    assert not state.has_waiting(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert state.has_waiting(tmp_path, TARGET, ISSUE, repo=BACK)

    state.mark_background(tmp_path, TARGET, ISSUE, [{"id": "b"}], now=40.0, repo=BACK)
    state.clear_turn_markers(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert state.read_background(tmp_path, TARGET, ISSUE, repo=FRONT) is None
    assert state.read_background(tmp_path, TARGET, ISSUE, repo=BACK) is not None


def test_same_number_tasks_keep_independent_overrides_and_quarantine(tmp_path):
    execution_overrides.save(tmp_path, TARGET, ISSUE,
                             execution_overrides.ExecutionOverride("m1", True), repo=FRONT)
    assert execution_overrides.load(tmp_path, TARGET, ISSUE, repo=BACK) is None
    assert execution_overrides.load(tmp_path, TARGET, ISSUE, repo=FRONT).model == "m1"
    execution_overrides.delete(tmp_path, TARGET, ISSUE, repo=BACK)
    assert execution_overrides.load(tmp_path, TARGET, ISSUE, repo=FRONT) is not None

    failures.write_quarantine(tmp_path, TARGET, ISSUE, "example/frontend", 99, "fp",
                              repo=FRONT)

    class ClosedGithub:
        def issue_state(self, repo, number):
            return "OPEN"

    assert failures.check_quarantine(tmp_path, ClosedGithub(), TARGET, ISSUE, repo=FRONT)
    assert not failures.check_quarantine(tmp_path, ClosedGithub(), TARGET, ISSUE, repo=BACK)


def test_same_number_tasks_have_distinct_failure_and_archive_identities(tmp_path):
    def report(repo):
        return failures.FailureReport("session-crash", TARGET, ISSUE, "t", "boom",
                                      "", "cmd", "", repo=repo)

    assert failures.fingerprint(report(FRONT)) != failures.fingerprint(report(BACK))
    failures.fingerprint_path(tmp_path, failures.fingerprint(report(FRONT))) \
        .parent.mkdir(parents=True)
    failures.fingerprint_path(tmp_path, failures.fingerprint(report(FRONT))).touch()
    assert failures.reported(tmp_path, report(FRONT))
    assert not failures.reported(tmp_path, report(BACK))

    assert (state.archive_root(tmp_path, TARGET, ISSUE, repo=FRONT)
            != state.archive_root(tmp_path, TARGET, ISSUE, repo=BACK))
    state.archive(tmp_path, task(FRONT, Stage.DONE))
    assert state.load_archived(tmp_path, TARGET, ISSUE, repo=BACK) is None
    assert state.load_archived(tmp_path, TARGET, ISSUE, repo=FRONT).repo == FRONT


def test_repeated_save_does_not_redeliver_or_duplicate_messages(tmp_path):
    save(tmp_path, task(FRONT))
    m = messages.append(tmp_path, TARGET, ISSUE, "hello", "operator", repo=FRONT)
    messages.mark_delivered(tmp_path, TARGET, ISSUE, [m.id], repo=FRONT)
    save(tmp_path, task(FRONT))
    save(tmp_path, task(FRONT, Stage.PR_OPEN))
    got = messages.all_messages(tmp_path, TARGET, ISSUE, repo=FRONT)
    assert [(x.text, bool(x.delivered_at)) for x in got] == [("hello", True)]
    assert len(load_all(tmp_path)) == 1


def _legacy(tmp_path, target, issue=5):
    """A record written before qualification: no repo."""
    save(tmp_path, task("", target=target, issue=issue))
    messages.append(tmp_path, target, issue, "old note", "operator")
    root = state.archive_root(tmp_path, target, issue)
    (root / "content").mkdir(parents=True)
    (root / "content" / "spec1").write_text("kept")


def test_migration_keeps_messages_and_stored_artifacts_and_is_repeatable(tmp_path):
    _legacy(tmp_path, "solo")
    for _ in range(2):
        state.migrate_legacy(tmp_path, {"solo": "example/solo"})
        moved = load(tmp_path, "solo", 5, repo="example/solo")
        assert moved.repo == "example/solo" and moved.title == "title of "
        assert [m.text for m in messages.all_messages(tmp_path, "solo", 5,
                                                      repo="example/solo")] == ["old note"]
        p = task_artifacts.content_path(tmp_path, "solo", 5, "spec1", repo="example/solo")
        assert p is not None and p.read_text() == "kept"
        assert len(load_all(tmp_path)) == 1


def test_legacy_record_without_a_proven_mapping_stays_unresolved(tmp_path):
    _legacy(tmp_path, TARGET, issue=ISSUE)       # shop has two repositories
    state.migrate_legacy(tmp_path, {"solo": "example/solo"})

    assert load(tmp_path, TARGET, ISSUE, repo=FRONT) is None
    assert load(tmp_path, TARGET, ISSUE, repo=BACK) is None
    assert [(t.target, t.issue, t.repo) for t in load_all(tmp_path)] == [(TARGET, ISSUE, "")]
    assert [m.text for m in messages.all_messages(tmp_path, TARGET, ISSUE)] == ["old note"]


def test_interrupted_migration_keeps_originals_and_reruns(tmp_path, monkeypatch):
    _legacy(tmp_path, "solo")
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("interrupted")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        state.migrate_legacy(tmp_path, {"solo": "example/solo"})
    monkeypatch.setattr(os, "replace", real)

    # The originals stay whole until their replacement is verified.
    assert load(tmp_path, "solo", 5).title == "title of "
    assert [m.text for m in messages.all_messages(tmp_path, "solo", 5)] == ["old note"]
    assert (state.archive_root(tmp_path, "solo", 5) / "content" / "spec1").read_text() == "kept"

    state.migrate_legacy(tmp_path, {"solo": "example/solo"})
    assert [(t.repo) for t in load_all(tmp_path)] == ["example/solo"]
    assert [m.text for m in messages.all_messages(tmp_path, "solo", 5,
                                                  repo="example/solo")] == ["old note"]
    assert task_artifacts.content_path(tmp_path, "solo", 5, "spec1",
                                       repo="example/solo").read_text() == "kept"


def test_review_answer_for_one_task_is_not_consumed_by_its_sibling(tmp_path):
    req = AnswersRequest(path="page.html", fingerprint="rev-1")
    tasks = {FRONT: task(FRONT, operator_request=req, worktree=str(tmp_path / "f")),
             BACK: task(BACK, operator_request=req, worktree=str(tmp_path / "b"))}
    payload = {"revision": "rev-1", "submit": "approve", "answers": {}}
    mine = intents.write_intent(tmp_path, "answers", TARGET, ISSUE, payload, "op", 1, repo=FRONT)
    other = intents.write_intent(tmp_path, "answers", TARGET, ISSUE, payload, "op", 2, repo=BACK)
    listed = {i.repo: i for i in intents.list_intents(tmp_path)}

    assert answers.request_reason(tasks[FRONT], listed[FRONT], "rev-1") is None
    assert answers.request_reason(tasks[FRONT], listed[BACK], "rev-1") is not None
    kept = answers.without_superseded(list(listed.values()), lambda i: tasks[i.repo])
    assert {i.repo for i in kept} == {FRONT, BACK}
    assert mine.exists() and other.exists()


def test_superseded_answers_are_resolved_per_repository(tmp_path):
    req = AnswersRequest(path="page.html", fingerprint="rev-1")
    tasks = {r: task(r, operator_request=req, worktree=str(tmp_path / r.split("/")[1]))
             for r in (FRONT, BACK)}
    payload = {"revision": "rev-1", "submit": "approve", "answers": {}}
    written = {r: [intents.write_intent(tmp_path, "answers", TARGET, ISSUE, payload,
                                        "op", ms, repo=r) for ms in (1, 2, 3)]
               for r in (FRONT, BACK)}

    pending = intents.list_intents(tmp_path)
    kept = answers.without_superseded(pending, lambda i: tasks[i.repo])

    for r in (FRONT, BACK):
        mine = [i for i in kept if i.repo == r]
        assert len(mine) == 1                       # one winner per repository
        assert sum(p.exists() for p in written[r]) == 1
        assert mine[0].path.exists()


def test_stale_revision_is_still_rejected_per_task(tmp_path):
    req = AnswersRequest(path="page.html", fingerprint="rev-2")
    t = task(FRONT, operator_request=req, worktree=str(tmp_path))
    intents.write_intent(tmp_path, "answers", TARGET, ISSUE,
                         {"revision": "rev-1", "submit": "approve"}, "op", 1, repo=FRONT)
    (stale,) = intents.list_intents(tmp_path)
    assert stale.repo == FRONT
    assert answers.request_reason(t, stale, "rev-2") == "stale revision"


@pytest.mark.parametrize("request_cls", [state.PlanApprovalRequest, AnswersRequest])
def test_request_changed_on_disk_is_rejected_though_intent_matches_stored_revision(
        tmp_path, request_cls):
    # The intent names the stored revision, but the page or plan changed since.
    t = task(FRONT, operator_request=request_cls(path="page.html", fingerprint="rev-1"),
             worktree=str(tmp_path))
    intents.write_intent(tmp_path, "answers", TARGET, ISSUE,
                         {"revision": "rev-1", "submit": "approve"}, "op", 1, repo=FRONT)
    (current,) = intents.list_intents(tmp_path)
    assert answers.request_reason(t, current, "rev-1") is None
    assert answers.request_reason(t, current, "rev-2") == "stale revision"


def test_migration_carries_the_waiting_marker_of_a_bare_issue_only_task(tmp_path):
    _legacy(tmp_path, "solo")
    (tmp_path / "task-solo-5.json").rename(tmp_path / "task-5.json")
    (tmp_path / "waiting-5").touch()
    assert state.has_waiting(tmp_path, "solo", 5)

    state.migrate_legacy(tmp_path, {"solo": "example/solo"})

    assert state.has_waiting(tmp_path, "solo", 5, repo="example/solo")


def _legacy_with_sidecars(tmp_path, target="solo", issue=5):
    _legacy(tmp_path, target, issue)
    state.mark_waiting(tmp_path, target, issue)
    execution_overrides.save(tmp_path, target, issue,
                             execution_overrides.ExecutionOverride("m1", True))
    failures.write_quarantine(tmp_path, target, issue, "example/solo", 99, "fp")
    intents.write_intent(tmp_path, "answers", target, issue, {}, "op", 1)


def test_migration_carries_sidecars_and_intent_ownership(tmp_path):
    _legacy_with_sidecars(tmp_path)
    state.migrate_legacy(tmp_path, {"solo": "example/solo"})
    repo = "example/solo"

    class OpenGithub:
        def issue_state(self, repo, number):
            return "OPEN"

    assert state.has_waiting(tmp_path, "solo", 5, repo=repo)
    assert execution_overrides.load(tmp_path, "solo", 5, repo=repo).model == "m1"
    assert failures.check_quarantine(tmp_path, OpenGithub(), "solo", 5, repo=repo)
    assert [i.repo for i in intents.list_intents(tmp_path)] == [repo]


def test_migration_moves_a_bare_issue_only_task_record(tmp_path):
    _legacy(tmp_path, "solo")
    (tmp_path / "task-solo-5.json").rename(tmp_path / "task-5.json")
    state.migrate_legacy(tmp_path, {"solo": "example/solo"})

    assert load(tmp_path, "solo", 5, repo="example/solo").title == "title of "
    assert [t.repo for t in load_all(tmp_path)] == ["example/solo"]


def test_migration_retry_keeps_messages_written_and_delivered_meanwhile(tmp_path, monkeypatch):
    _legacy(tmp_path, "solo")
    done = messages.append(tmp_path, "solo", 5, "second", "operator")

    def interrupted(*a, **kw):
        raise OSError("interrupted")

    # The queue copy lands, then the run stops before the originals go.
    monkeypatch.setattr(state.shutil, "copytree", interrupted)
    with pytest.raises(OSError):
        state.migrate_legacy(tmp_path, {"solo": "example/solo"})
    monkeypatch.undo()

    messages.append(tmp_path, "solo", 5, "after restart", "operator")
    messages.mark_delivered(tmp_path, "solo", 5, [done.id])
    state.migrate_legacy(tmp_path, {"solo": "example/solo"})

    got = messages.all_messages(tmp_path, "solo", 5, repo="example/solo")
    assert sorted(m.text for m in got) == ["after restart", "old note", "second"]
    assert {m.text: bool(m.delivered_at) for m in got} == {
        "old note": False, "second": True, "after restart": False}


def _wake_markers(c):
    return sorted(p.name for p in Path(c.state_dir).glob(f"{state.WAKE_BLOCKED_PREFIX}*"))


def test_same_number_tasks_have_independent_wake_blocked_markers(tmp_path, monkeypatch):
    import dispatcher.main as main
    from tests.test_main import FakeSessions, deps, patch_usage, patch_workspace
    from tests.test_multi_project_capacity import mk_task, two_target_cfg

    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "factorial", 5, slot=2)
    base = dict(park=state.PARK_WAKE, slot=3,
                updated_at="2026-07-21T10:00:00+00:00")
    mk_task(c, "portfolio_eval", 12, **base, repo=FRONT)
    save(c.state_dir, replace(load(c.state_dir, "portfolio_eval", 12, repo=FRONT),
                              repo=BACK, branch="agent/task-12-back"))
    main.run_pass(c, deps(sess=FakeSessions(alive={1, 2, 5})))

    markers = _wake_markers(c)
    assert len(markers) == 2 and len(set(markers)) == 2
    assert {m for m in markers if "frontend" in m} and {m for m in markers if "backend" in m}

    # The front task stops starving: only its own marker goes, the sibling keeps its.
    save(c.state_dir, replace(load(c.state_dir, "portfolio_eval", 12, repo=FRONT), park=""))
    main.run_pass(c, deps(sess=FakeSessions(alive={1, 2, 5})))
    left = _wake_markers(c)
    assert len(left) == 1 and "backend" in left[0]


def test_standalone_intent_migrates_to_its_proven_repository(tmp_path):
    intents.write_intent(tmp_path, "answers", "solo", 5, {}, "op", 1)
    state.migrate_legacy(tmp_path, {"solo": "example/solo"})
    assert [i.repo for i in intents.list_intents(tmp_path)] == ["example/solo"]


def test_migration_does_not_retire_an_original_for_an_unreadable_destination(tmp_path):
    _legacy(tmp_path, "solo")
    dst = state._path(tmp_path, "solo", 5, "example/solo")
    dst.write_text("{}")
    state.migrate_legacy(tmp_path, {"solo": "example/solo"})

    # A replacement that cannot be read is no replacement: the original stays.
    assert load(tmp_path, "solo", 5).title == "title of "
    assert [m.text for m in messages.all_messages(tmp_path, "solo", 5)] == ["old note"]


def test_migration_retry_keeps_artifact_updated_after_interrupted_copy(tmp_path, monkeypatch):
    _legacy(tmp_path, "solo")
    monkeypatch.setattr(state, "_verify_tree",
                        lambda *a: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        state.migrate_legacy(tmp_path, {"solo": "example/solo"})
    monkeypatch.undo()

    state.archive_root(tmp_path, "solo", 5, repo="example/solo")
    (task_artifacts.content_path(tmp_path, "solo", 5, "spec1", repo="example/solo")
     ).write_text("updated artifact after restart")
    state.migrate_legacy(tmp_path, {"solo": "example/solo"})

    p = task_artifacts.content_path(tmp_path, "solo", 5, "spec1", repo="example/solo")
    assert p.read_text() == "updated artifact after restart"


def test_migration_retry_keeps_task_and_session_updated_in_the_retained_original(
        tmp_path, monkeypatch):
    _legacy(tmp_path, "solo")
    state.write_session(tmp_path, "solo", 5, SessionRecord("sid-a", "implement"))
    monkeypatch.setattr(state, "_migrate_archive",
                        lambda *a: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        state.migrate_legacy(tmp_path, {"solo": "example/solo"})
    monkeypatch.undo()

    save(tmp_path, task("", Stage.PR_OPEN, target="solo", issue=5))
    state.write_session(tmp_path, "solo", 5, SessionRecord("sid-b", "implement"))
    state.migrate_legacy(tmp_path, {"solo": "example/solo"})

    assert load(tmp_path, "solo", 5, repo="example/solo").stage == Stage.PR_OPEN
    assert state.read_session(tmp_path, "solo", 5, repo="example/solo").session_id == "sid-b"
