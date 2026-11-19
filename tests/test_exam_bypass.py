"""The owner's bypass labels, through `POST /api/exam` and `exams.work` with injected fakes.

Interface under test (defined here, built by the ticket), on top of test_exam_generation.py:
    Config.operator_login: str = ""   (`operator_login` in targets.yaml; the scenarios' "repo owner")
    github.pr(repo, n) also returns "labels": [label names on the PR now]
    github.label_events(repo, n) -> [{"event": "labeled" | "unlabeled", "label": str,
        "actor": login}], oldest first (paging is the github module's business)
    github.file(repo, ref, ".pr-exam.json") -> str | None, read at the PR's base ref only
    pr_exam.config(path: Path) -> {"errors": [str], "enabled": bool,
        "labels": {label: "skip" | "defer"}}   (what `pr_exam.py config` prints for the file)
    A request names no label and no mode: the box reads labels, events and config itself.
    Skip -> state "skipped", success "bypassed", no session. Defer -> exam generated, `debt`
    true, success "deferred". A bypass covers the PR on any head and is posted again on a push.
"""
import dataclasses
import json

from tests.test_exam_generation import HEAD, HEADERS, REPO, Github
from tests.test_exam_grading import Exam, Scoring, Session as GradingSession, graded_once, opened

NEW = "def456"
OWNER, BOT, MEMBER = "jesdi", "some-bot[bot]", "org-member"
SKIP, DEFER = "exam:skip", "exam:defer"
CONFIG = {"labels": {SKIP: "skip", DEFER: "defer"}}


class Hub(Github):
    def __init__(self):
        super().__init__()
        self.configs = {"main": json.dumps(CONFIG)}
        self.described = []

    def post_status(self, repo, sha, context, state, description):
        super().post_status(repo, sha, context, state, description)
        self.described.append((sha, state, description))


def box_for(tmp_path, monkeypatch, label=None, actor=OWNER, operator=OWNER):
    box = Exam(tmp_path, monkeypatch)
    box.github = Hub()
    box.cfg = dataclasses.replace(box.cfg, operator_login=operator)
    if label:
        box.github.labels = [label]
        box.github.events = [{"event": "labeled", "label": label, "actor": actor}]
    return box


def successes(box, sha=None):
    return [d for d in box.github.described if d[1] == "success" and sha in (None, d[0])]


def read_base_config(box):
    assert (REPO, "main", ".pr-exam.json") in box.github.files or (
        "acme/widgets", "main", ".pr-exam.json") in box.github.files


def test_the_owners_skip_is_honoured_with_no_session(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP)
    box.request()
    box.work()
    assert successes(box) == [(HEAD, "success", "bypassed")]
    assert box.read().json()["state"] == "skipped"
    assert box.session.calls == []


def test_a_skip_added_by_a_bot_changes_nothing(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP, actor=BOT)
    box.request()
    box.work()
    read_base_config(box)
    assert successes(box) == []
    assert (REPO, HEAD, "exam", "pending") in box.posted()
    assert box.read().json()["state"] == "open"
    assert len(box.session.calls) == 1


def test_a_label_the_owner_added_and_a_bot_removed_and_re_added_is_refused(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP)
    box.github.events += [{"event": "unlabeled", "label": SKIP, "actor": BOT},
                          {"event": "labeled", "label": SKIP, "actor": BOT}]
    box.request()
    box.work()
    read_base_config(box)
    assert successes(box) == []
    assert box.read().json()["state"] == "open"
    assert len(box.session.calls) == 1


def test_the_owners_defer_posts_deferred_and_opens_an_exam_that_counts_as_debt(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, DEFER)
    box.request()
    box.work()
    body = box.read().json()
    assert body["state"] == "open" and body["debt"] is True
    assert len(body["exam"]["questions"]) == 5
    assert len(box.session.calls) == 1
    assert box.github.described[-1] == (HEAD, "success", "deferred")


def post_for(box, repo):
    response = box.client.post("/api/exam", headers=HEADERS, json={"repo": repo, "pr": 240})
    assert response.status_code == 200
    box.work()
    return box.client.get(f"/api/exam/{repo}/240", headers=HEADERS).json()


def test_in_an_organization_repo_the_configured_operator_is_honoured(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP, actor=OWNER)
    assert post_for(box, "acme/widgets")["state"] == "skipped"
    assert successes(box) == [(HEAD, "success", "bypassed")]


def test_in_an_organization_repo_another_member_is_not_honoured(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP, actor=MEMBER)
    assert post_for(box, "acme/widgets")["state"] == "open"
    read_base_config(box)
    assert successes(box) == []
    assert len(box.session.calls) == 1


def test_an_unset_operator_login_is_an_error_naming_the_key(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP, operator="")
    box.request()
    box.work()
    body = box.read().json()
    assert body["state"] == "error"
    assert "operator_login" in body["detail"]
    assert successes(box) == []


def test_a_skip_is_posted_again_on_a_new_head_with_no_session(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP)
    box.request()
    box.work()
    box.github.head = NEW
    box.request()
    box.work()
    assert (NEW, "success", "bypassed") in box.github.described
    assert box.read().json()["state"] == "skipped"
    assert box.session.calls == []


def test_a_defer_keeps_its_exam_and_posts_deferred_on_a_new_head(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, DEFER)
    box.request()
    box.work()
    box.github.head = NEW
    box.request()
    box.work()
    assert (NEW, "success", "deferred") in box.github.described
    body = box.read().json()
    assert body["state"] == "open" and body["debt"] is True and body["exam"]["questions"]
    assert len(box.session.calls) == 1


def test_a_label_missing_from_the_config_bypasses_nothing(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, "exam:other")
    box.request()
    box.work()
    read_base_config(box)
    assert successes(box) == []
    assert box.read().json()["state"] == "open"
    assert len(box.session.calls) == 1


def test_a_label_mapped_only_on_the_pr_branch_bypasses_nothing(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP)
    box.github.configs = {HEAD: json.dumps(CONFIG)}
    box.request()
    box.work()
    read_base_config(box)
    assert successes(box) == []
    assert box.read().json()["state"] == "open"
    assert len(box.session.calls) == 1
    assert all(ref == "main" for _, ref, path in box.github.files if path == ".pr-exam.json")


def test_a_config_the_skill_refuses_is_an_error_with_its_errors_and_no_success(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP)
    box.github.configs = {"main": json.dumps({**CONFIG, "bogus": 1})}
    box.request()
    box.work()
    body = box.read().json()
    assert body["state"] == "error"
    assert "unknown key: bogus" in body["detail"]
    assert successes(box) == []


def test_a_config_command_exiting_nonzero_is_an_error_with_its_output_and_no_success(tmp_path, monkeypatch):
    """The production adapter raises when `pr_exam.py config` exits nonzero."""
    import subprocess
    from dispatcher.exam_runner import PrExam

    class Refusing(Scoring):
        def config(self, path):
            fake = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "unknown key: bogus\n")
            return PrExam(run=fake).config(path)

    box = box_for(tmp_path, monkeypatch, SKIP)
    box.pr_exam = Refusing()
    box.request()
    box.work()
    body = box.read().json()
    assert body["state"] == "error"
    assert "unknown key: bogus" in body["detail"]
    assert successes(box) == []


def test_a_disabled_repo_runs_no_session_and_posts_no_status(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    box.github.configs = {"main": json.dumps({"enabled": False})}
    box.request()
    box.work()
    assert box.session.calls == []
    assert box.github.statuses == []


def unlabel(box, label):
    box.github.labels = []
    box.github.events.append({"event": "unlabeled", "label": label, "actor": OWNER})
    box.request()
    box.work()


def test_removing_the_owners_defer_puts_the_head_back_to_pending(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, DEFER)
    box.request()
    box.work()
    assert box.github.described[-1] == (HEAD, "success", "deferred")
    unlabel(box, DEFER)
    assert box.github.described[-1][:2] == (HEAD, "pending")
    body = box.read().json()
    assert body["state"] == "open" and body["debt"] is False


def test_removing_the_owners_skip_puts_the_head_back_to_pending_and_examines_it(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, SKIP)
    box.request()
    box.work()
    assert box.github.described[-1] == (HEAD, "success", "bypassed")
    unlabel(box, SKIP)
    assert box.github.described[-1][:2] == (HEAD, "pending")
    assert box.read().json()["state"] == "open"


def test_removing_the_owners_defer_after_a_pass_keeps_the_success(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, DEFER)
    box.session = GradingSession(grades={"t0": "pass", "t1": "pass"})
    assert graded_once(box, opened(box))["state"] == "passed"
    assert box.github.described[-1] == (HEAD, "success", "Exam passed")
    unlabel(box, DEFER)
    assert box.github.described[-1] == (HEAD, "success", "Exam passed")
    assert box.read().json()["state"] == "passed"
