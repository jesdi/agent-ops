"""Acceptance tests for ticket 21 (project-onboarding): migrating an existing
project without losing live work, driven through the MIGRATE seam.

Spec: specs/project-onboarding/spec.md R21, S47, S48 (and S29's legacy excess).
Ticket: .agent/tickets/21-existing-project-migration.md.

Interface these tests define (the ticket leaves it open). Three entry points in
`dispatcher.main`, each taking the state directory and an `infra` edge (the
agent-ops-infra side of the contract; faked here):
- `migration_check(state_dir, infra) -> dict`: a read. Keys:
  `consumers` (ids of live legacy consumers, from `infra.legacy_consumers`),
  `mappings` ({target: repo}, from `infra.mapping()`), `credentials`
  (`infra.credentials(target)` per mapped target), `unfinished` ({target: n},
  counted from the task records on disk now) and `over_limit`
  ({target: n above `infra.limit(target)`}, only targets above their limit),
  `unmapped` ([(target, issue)] of legacy records no proven mapping covers).
- `migration_apply(state_dir, infra, rehearse=False) -> dict`: idempotent.
  Keys: `completed` (bool), `blockers` ([str]), `retired` ([target] whose shared
  credential was revoked this call), `switched` ([target]).
  Order: `infra.select_revision()`, then (only if that holds) `infra.apply_schema()`,
  then state migration, then for each mapped target `infra.test_access(target)`
  (list of denied operations, empty = pass); on pass `infra.switch(target)` (later
  sessions use scoped access); `infra.revoke_shared(target)` only when the access
  test passed and `infra.legacy_consumers(target)` is empty.
  `rehearse=True` migrates a COPY of the state and calls no mutating infra method;
  `state_dir` is untouched.
"""
import shutil
from pathlib import Path

from dispatcher import main, messages, state
from dispatcher.state import Stage
from tests.webfakes import make_task

TARGET, REPO = "portfolio_eval", "example/portfolio_eval"
LIVE = {"id": "task-portfolio_eval-7", "kind": "session"}


class Infra:
    def __init__(self, consumers=(), denied=(), revision_ok=True, limit=10):
        self.consumers, self.denied = list(consumers), list(denied)
        self.revision_ok, self._limit = revision_ok, limit
        self.calls, self.shared = [], "present"

    def mapping(self):
        return {TARGET: REPO}

    def limit(self, target):
        return self._limit

    def legacy_consumers(self, target):
        return list(self.consumers)

    def credentials(self, target):
        return {"shared": self.shared, "scoped": "present"}

    def test_access(self, target):
        self.calls.append("test_access")
        return list(self.denied)

    def select_revision(self):
        self.calls.append("select_revision")
        return self.revision_ok

    def apply_schema(self):
        self.calls.append("apply_schema")

    def switch(self, target):
        self.calls.append("switch")

    def revoke_shared(self, target):
        self.calls.append("revoke_shared")
        self.shared = "retired"


def legacy_task(issue=7, **kw):
    return make_task(issue=issue, target=TARGET, pr_number=100 + issue, **kw)


def seed(tmp_path, issues=(7,)):
    sd = tmp_path / "state"
    for i in issues:
        state.save(sd, legacy_task(i))
    done = legacy_task(3, stage=Stage.DONE)
    state.save(sd, done)
    state.archive(sd, done)
    (state.archive_root(sd, TARGET, 3) / "plan.md").write_text("the retained plan")
    return sd


def test_apply_lets_the_live_session_finish_and_retires_the_shared_credential_last(tmp_path):  # S47
    sd, infra = seed(tmp_path), Infra(consumers=[LIVE])
    live = state.SessionRecord(session_id="live-session", stage="plan")
    state.write_session(sd, TARGET, 7, live)  # the running session's conversation
    first = main.migration_apply(sd, infra)
    assert state.read_session(sd, TARGET, 7, repo=REPO) == live  # still reachable under the new identity
    assert state.owns_task_session(sd, TARGET, 7, REPO)  # a legacy completion callback still finds its owner
    assert state.session_owner(sd, TARGET, 7) == REPO
    assert infra.calls.count("switch") == 1 and "revoke_shared" not in infra.calls
    assert first["retired"] == [] and infra.shared == "present"
    assert state.load(sd, TARGET, 7, repo=REPO).title == "Task 7"
    assert (state.archive_root(sd, TARGET, 3, repo=REPO) / "plan.md").read_text() == "the retained plan"
    infra.consumers = []  # the old session finished
    second = main.migration_apply(sd, infra)
    assert second["retired"] == [TARGET] and infra.shared == "retired"
    assert second["completed"] is True


def test_apply_keeps_the_owner_of_a_live_session_that_has_not_called_back_yet(tmp_path):  # S47
    sd, infra = seed(tmp_path), Infra(consumers=[LIVE])  # running, no session record written yet
    main.migration_apply(sd, infra)
    assert state.owns_task_session(sd, TARGET, 7, REPO)  # its first completion callback finds its owner
    assert state.session_owner(sd, TARGET, 7) == REPO


def test_failed_replacement_write_keeps_working_credentials_and_reports_the_blocker(tmp_path):  # S48
    sd, infra = seed(tmp_path), Infra(denied=["board write"])
    report = main.migration_apply(sd, infra)
    assert report["completed"] is False
    assert any("board write" in b and TARGET in b for b in report["blockers"])
    assert "revoke_shared" not in infra.calls and "switch" not in infra.calls
    assert infra.shared == "present" and report["retired"] == []
    assert state.load(sd, TARGET, 7) is not None or state.load(sd, TARGET, 7, repo=REPO)


def test_check_reports_the_live_state_not_a_historical_snapshot(tmp_path):
    sd = seed(tmp_path, issues=range(10, 23))  # 13 unfinished
    report = main.migration_check(sd, Infra(consumers=[LIVE]))
    assert report["consumers"] == [LIVE["id"]]
    assert report["mappings"] == {TARGET: REPO}
    assert report["credentials"][TARGET] == {"shared": "present", "scoped": "present"}
    assert report["unfinished"] == {TARGET: 13} and report["over_limit"] == {TARGET: 3}
    for issue in range(10, 20):  # tasks complete
        state.save(sd, legacy_task(issue, stage=Stage.DONE))
    report = main.migration_check(sd, Infra())
    assert report["unfinished"] == {TARGET: 3} and report["over_limit"] == {}
    assert report["consumers"] == []


def test_repeated_apply_preserves_identity_and_artifacts(tmp_path):
    sd, infra = seed(tmp_path), Infra()
    main.migration_apply(sd, infra)
    before = state.load(sd, TARGET, 7, repo=REPO)
    main.migration_apply(sd, infra)
    after = state.load(sd, TARGET, 7, repo=REPO)
    assert (after.issue, after.repo, after.branch) == (7, REPO, before.branch)
    assert before.pr_number == after.pr_number == 107  # the PR reference survives every apply
    assert state.load(sd, TARGET, 7) is None
    assert [t.repo for t in state.load_all(sd) if t.issue == 7] == [REPO]  # not duplicated
    assert (state.archive_root(sd, TARGET, 3, repo=REPO) / "plan.md").read_text() == "the retained plan"


def test_ambiguous_records_are_reported_and_never_discarded(tmp_path):
    sd = seed(tmp_path)
    state.save(sd, make_task(issue=9, target="ghost"))  # no proven mapping
    assert main.migration_check(sd, Infra())["unmapped"] == [("ghost", 9)]
    report = main.migration_apply(sd, Infra())
    assert state.load(sd, "ghost", 9) is not None
    assert report["completed"] is False and any("ghost" in b for b in report["blockers"])


def test_compatible_revision_is_selected_before_the_schema_and_blocks_it_when_absent(tmp_path):
    sd, infra = seed(tmp_path), Infra()
    main.migration_apply(sd, infra)
    assert infra.calls.index("select_revision") < infra.calls.index("apply_schema")
    sd2, bad = seed(tmp_path / "b"), Infra(revision_ok=False)
    report = main.migration_apply(sd2, bad)
    assert "apply_schema" not in bad.calls and report["completed"] is False
    assert "revoke_shared" not in bad.calls


def test_rehearsal_migrates_a_copy_and_changes_nothing_live(tmp_path):
    sd, infra = seed(tmp_path), Infra()
    shutil.copytree(sd, tmp_path / "snapshot")
    report = main.migration_apply(sd, infra, rehearse=True)
    assert report["completed"] is True
    assert not {"apply_schema", "switch", "revoke_shared"} & set(infra.calls)
    assert state.load(sd, TARGET, 7) is not None and state.load(sd, TARGET, 7, repo=REPO) is None
    names = lambda root: sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))
    assert names(sd) == names(tmp_path / "snapshot")


def bare_queue(sd, issue=7, text="operator instruction"):
    message = messages.append(sd, TARGET, issue, text, "operator")
    path = sd / "messages" / f"{issue}.jsonl"
    (sd / "messages" / f"{state.task_key(TARGET, issue)}.jsonl").rename(path)
    return path, message


def test_apply_preserves_bare_messages_and_delivery_history_on_retry(tmp_path):
    sd = seed(tmp_path)
    bare, queued = bare_queue(sd)
    delivered = messages.append(sd, TARGET, 7, "already read", "operator", repo=REPO)
    messages.mark_delivered(sd, TARGET, 7, [delivered.id], repo=REPO)
    infra = Infra()
    for _ in range(2):
        assert main.migration_apply(sd, infra)["completed"]
        assert messages.undelivered(sd, TARGET, 7, repo=REPO) == [queued]
        assert len(messages.all_messages(sd, TARGET, 7, repo=REPO)) == 2
    assert not bare.exists()


def test_bare_queue_without_task_is_migrated_and_rehearsal_preserves_source(tmp_path):
    sd = seed(tmp_path)
    bare, queued = bare_queue(sd, issue=99)
    assert main.migration_apply(sd, Infra(), rehearse=True)["completed"]
    assert bare.exists()
    assert main.migration_apply(sd, Infra())["completed"]
    assert messages.undelivered(sd, TARGET, 99, repo=REPO) == [queued]


def test_ambiguous_bare_queue_blocks_check_and_apply_without_discarding(tmp_path):
    class MultiInfra(Infra):
        def mapping(self):
            return {TARGET: REPO, "other": "example/other"}

    sd = seed(tmp_path)
    bare, _ = bare_queue(sd)
    before = bare.read_bytes()
    infra = MultiInfra()
    assert main.migration_check(sd, infra)["unmapped_messages"] == ["messages/7.jsonl"]
    for _ in range(2):
        report = main.migration_apply(sd, infra)
        assert not report["completed"]
        assert any("messages/7.jsonl" in b for b in report["blockers"])
        assert bare.read_bytes() == before
        assert messages.undelivered(sd, TARGET, 7, repo=REPO) == []


def test_bare_and_target_queues_merge_before_qualification(tmp_path):
    sd = seed(tmp_path)
    bare, queued = bare_queue(sd)
    delivered = messages.append(sd, TARGET, 7, "already read", "operator")
    messages.mark_delivered(sd, TARGET, 7, [delivered.id])
    assert main.migration_apply(sd, Infra())["completed"]
    assert not bare.exists()
    assert messages.undelivered(sd, TARGET, 7, repo=REPO) == [queued]
    assert len(messages.all_messages(sd, TARGET, 7, repo=REPO)) == 2


def test_unmapped_legacy_project_prevents_bare_queue_attribution(tmp_path):
    from tests.webfakes import make_task

    sd = seed(tmp_path)
    bare, _ = bare_queue(sd)
    state.save(sd, make_task(issue=8, target="unknown"))
    assert main.migration_check(sd, Infra())["unmapped_messages"] == ["messages/7.jsonl"]
    assert not main.migration_apply(sd, Infra())["completed"]
    assert bare.exists()
