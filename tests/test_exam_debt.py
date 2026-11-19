"""Exam debt, cleanup and the daily sweep, through `python -m dispatcher.main --exam-sweep`,
`POST /api/exam`, `GET /api/exams`, `GET /api/exam/{owner}/{name}/{pr}` and `exams.work`.

Interface under test (defined here, built by the ticket), on top of test_exam_bypass.py and
test_exam_records.py:
    github.pr(repo, n) -> "state": "open" | "closed", "merged": bool   (as the fakes already do)
    GET /api/exams -> 200, a JSON list, one object per exam that is open debt:
        {"repo", "pr", "head_sha", "state", "debt"}. A passed exam is not in it.
    `main()` with `--exam-sweep` runs `exam_runner.sweep(cfg, deps)` once and returns (exit 0):
        it reads every exam on the box, asks `exam_runner.Github` for each PR and reconciles it,
        through `exam_runner.session`, `exam_runner.PrExam` and `exam_runner.fetch_all`, as
        `--exam-run` does. A request for a PR runs the same reconcile (`exams.work`).
    reconcile: merged + passed (or skipped)  -> exam page and data deleted, records kept, 404.
               closed, not merged            -> exam deleted, no record written.
               merged, exam open             -> stays `open` with `debt` true.
               merged, never an exam         -> nothing is created.
    Passing a debt exam writes its records and posts no status.
"""
import dataclasses
import sys
from types import SimpleNamespace

import pytest

from tests.test_exam_bypass import DEFER, OWNER, SKIP, Hub
from tests.test_exam_generation import HEADERS, NOW, REPO
from tests.test_exam_grading import RETRY
from tests.test_exam_pushes import NEW, passed_box
from tests.test_exam_records import PASS_ALL, Box

CLAUDE = "jesdi/claude-config"


class Gh(Hub):
    def __init__(self):
        super().__init__()
        self.prs, self.broken = {}, set()

    def pr(self, repo, n):
        if (repo, n) in self.broken:
            raise RuntimeError("GitHub is down for this PR")
        return {**super().pr(repo, n), **self.prs.get((repo, n), {})}

    def merge(self, repo, n):
        self.prs[(repo, n)] = {"state": "closed", "merged": True}

    def close(self, repo, n):
        self.prs[(repo, n)] = {"state": "closed", "merged": False}


def make(tmp_path, monkeypatch, label=None):
    box = Box(tmp_path, monkeypatch, grades=PASS_ALL)
    box.github = Gh()
    box.cfg = dataclasses.replace(box.cfg, operator_login=OWNER)
    if label:
        box.github.labels = [label]
        box.github.events = [{"event": "labeled", "label": label, "actor": OWNER}]
    return box


def sweep(box, monkeypatch):
    """`main()` with `--exam-sweep`, the production edges replaced by the box's fakes."""
    from dispatcher import exam_runner, main
    deps = SimpleNamespace(app=SimpleNamespace(registered=lambda: True), access=None,
                           clock=lambda: NOW,
                           notifier=SimpleNamespace(send=lambda *a, **k: None))
    monkeypatch.setattr(main, "_load", lambda path: (box.cfg, box.cfg))
    monkeypatch.setattr(main, "_deps", lambda cfg, dry_run: deps)
    monkeypatch.setattr(exam_runner, "Github", lambda *a, **k: box.github)
    monkeypatch.setattr(exam_runner, "PrExam", lambda *a, **k: box.pr_exam)
    monkeypatch.setattr(exam_runner, "session", lambda cfg, **k: box.session)
    monkeypatch.setattr(exam_runner, "fetch_all", lambda cfg: box.usages)
    monkeypatch.setattr(sys, "argv", ["agent-ops-dispatcher", "--config", "t.yaml", "--exam-sweep"])
    try:
        main.main()
    except SystemExit as stop:
        return stop.code or 0
    return 0


def request(box, repo, pr):
    assert box.client.post("/api/exam", headers=HEADERS, json={"repo": repo, "pr": pr}).status_code == 200
    box.work()


def read(box, repo, pr):
    return box.client.get(box.api(repo, pr), headers=HEADERS)


def listed(box):
    response = box.client.get("/api/exams", headers=HEADERS)
    assert response.status_code == 200
    return {(e["repo"], e["pr"]): e for e in response.json()}


def passed(box, repo, pr):
    body = box.open(repo, pr)
    box.grade(repo, pr, body)
    box.work()
    assert read(box, repo, pr).json()["state"] == "passed"


@pytest.mark.parametrize("how", ["request", "sweep"])
def test_a_merge_after_a_pass_deletes_the_exam_and_keeps_its_records(tmp_path, monkeypatch, how):
    box = make(tmp_path, monkeypatch)
    passed(box, REPO, 240)
    records = box.records(REPO)
    assert len(records) == 5
    box.github.merge(REPO, 240)
    if how == "request":
        request(box, REPO, 240)
    else:
        assert sweep(box, monkeypatch) == 0
    assert read(box, REPO, 240).status_code == 404
    assert (REPO, 240) not in listed(box)
    assert box.records(REPO) == records


def test_a_pr_closed_without_merging_loses_its_exam_and_writes_no_record(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch)
    assert box.open(REPO, 241)["state"] == "open"
    box.github.close(REPO, 241)
    assert sweep(box, monkeypatch) == 0
    assert read(box, REPO, 241).status_code == 404
    assert box.records(REPO) == []
    assert box.pr_exam.calls == []


def test_a_merge_while_the_exam_is_pending_becomes_debt_and_is_listed(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch)
    box.open(CLAUDE, 12)
    box.github.merge(CLAUDE, 12)
    assert sweep(box, monkeypatch) == 0
    body = read(box, CLAUDE, 12).json()
    assert body["state"] == "open" and body["debt"] is True
    assert listed(box)[(CLAUDE, 12)]["debt"] is True


def test_a_deferred_exam_whose_pr_is_merged_stays_open_debt_and_listed(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch, DEFER)
    box.open(REPO, 240)
    box.github.merge(REPO, 240)
    assert sweep(box, monkeypatch) == 0
    body = read(box, REPO, 240).json()
    assert body["state"] == "open" and body["debt"] is True
    assert listed(box)[(REPO, 240)]["debt"] is True


def test_passing_a_debt_exam_of_a_merged_pr_writes_records_posts_no_status_and_clears_it(
        tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch)
    box.open(CLAUDE, 12)
    box.github.merge(CLAUDE, 12)
    sweep(box, monkeypatch)
    assert (CLAUDE, 12) in listed(box)
    posted = list(box.github.statuses)
    box.grade(CLAUDE, 12, read(box, CLAUDE, 12).json())
    box.work()
    assert len(box.records(CLAUDE)) == 5
    assert box.github.statuses == posted
    assert (CLAUDE, 12) not in listed(box)


def test_a_deferred_exam_whose_pr_is_closed_without_merging_is_deleted(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch, DEFER)
    box.open(REPO, 240)
    box.github.close(REPO, 240)
    assert sweep(box, monkeypatch) == 0
    assert read(box, REPO, 240).status_code == 404
    assert (REPO, 240) not in listed(box)
    assert box.records(REPO) == []


def test_a_merged_pr_that_never_had_an_exam_is_not_turned_into_debt(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch)
    box.github.merge(REPO, 99)
    request(box, REPO, 99)
    assert sweep(box, monkeypatch) == 0
    assert (REPO, 99) not in listed(box)
    assert box.session.calls == [] and box.posted() == []


def test_the_sweep_exits_zero_with_no_exams(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch)
    assert sweep(box, monkeypatch) == 0
    assert listed(box) == {}


def test_a_github_error_on_one_pr_does_not_stop_the_sweep_of_the_others(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch)
    box.open(REPO, 240)
    box.open(REPO, 241)
    box.github.broken.add((REPO, 240))
    box.github.close(REPO, 241)
    sweep(box, monkeypatch)
    assert read(box, REPO, 241).status_code == 404
    assert read(box, REPO, 240).json()["state"] == "open"


def passed_then_pushed_and_merged(tmp_path, monkeypatch, delta):
    """A pass on the first head, then a push and a merge with no request in between."""
    box = passed_box(tmp_path, monkeypatch, delta=delta)
    diff, box.github = box.github.diff, Gh()
    box.github.diff = diff
    box.api = lambda repo, pr: f"/api/exam/{repo}/{pr}"
    box.github.head = NEW
    box.github.merge(REPO, 240)
    request(box, REPO, 240)
    return box


def test_a_pass_on_an_older_head_leaves_the_merged_heads_new_concept_as_debt(tmp_path, monkeypatch):
    box = passed_then_pushed_and_merged(tmp_path, monkeypatch, delta=[RETRY])
    body = read(box, REPO, 240).json()
    assert (body["state"], body["debt"], body["head_sha"]) == ("open", True, NEW)
    assert len(body["exam"]["questions"]) == 1
    assert listed(box)[(REPO, 240)]["debt"] is True
    assert not any(sha == NEW for _, sha, _, _ in box.posted())


def test_a_pass_on_an_older_head_is_judged_against_the_merged_head_before_cleanup(tmp_path, monkeypatch):
    box = passed_then_pushed_and_merged(tmp_path, monkeypatch, delta=[])
    assert len(box.session.deltas()) == 1
    assert read(box, REPO, 240).status_code == 404


def test_a_skip_whose_label_was_removed_before_the_merge_becomes_debt(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch, SKIP)
    request(box, REPO, 240)
    assert read(box, REPO, 240).json()["state"] == "skipped"
    box.github.labels = []
    box.github.merge(REPO, 240)
    assert sweep(box, monkeypatch) == 0
    body = read(box, REPO, 240).json()
    assert body["state"] == "open" and body["debt"] is True
    assert listed(box)[(REPO, 240)]["debt"] is True


def merged_debt(tmp_path, monkeypatch):
    box = make(tmp_path, monkeypatch)
    body = box.open(CLAUDE, 12)
    box.github.merge(CLAUDE, 12)
    sweep(box, monkeypatch)
    return box, body


def test_a_debt_exam_stays_listed_while_it_is_graded_and_after_a_failed_attempt(tmp_path, monkeypatch):
    box, body = merged_debt(tmp_path, monkeypatch)
    box.grade(CLAUDE, 12, body, wrong=("q0", "q1"))
    entry = listed(box)[(CLAUDE, 12)]
    assert (entry["state"], entry["debt"]) == ("grading", True)
    box.work()
    body = read(box, CLAUDE, 12).json()
    assert (body["state"], body["attempt"], body["debt"]) == ("open", 2, True)
    assert listed(box)[(CLAUDE, 12)]["debt"] is True


def test_a_debt_exam_whose_grading_failed_stays_listed(tmp_path, monkeypatch):
    import subprocess
    box, body = merged_debt(tmp_path, monkeypatch)
    box.session.grade_error = subprocess.CalledProcessError(1, "session")
    box.grade(CLAUDE, 12, body)
    box.work()
    entry = listed(box)[(CLAUDE, 12)]
    assert (entry["state"], entry["debt"]) == ("error", True)


def test_a_passed_debt_exam_of_a_merged_pr_is_deleted_with_its_records_kept(tmp_path, monkeypatch):
    box, body = merged_debt(tmp_path, monkeypatch)
    posted = list(box.github.statuses)
    box.grade(CLAUDE, 12, body)
    box.work()
    assert read(box, CLAUDE, 12).status_code == 404
    assert len(box.records(CLAUDE)) == 5
    assert box.github.statuses == posted
