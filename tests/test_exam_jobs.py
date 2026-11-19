"""The exam job's claim, the runner the pass launches instead of doing exam work, and the
pinned `pr_exam.py` on the delta request the host writes."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dispatcher import exam_runner, exams
from tests.test_exam_generation import NOW, REPO
from tests.test_exam_pushes import NEW
from tests.webfakes import make_config


def test_a_delayed_worker_does_not_claim_an_exam_that_already_opened(tmp_path):
    cfg = make_config(tmp_path)
    stale = exams.request(cfg.state_dir, "o/r", 1)   # what the delayed worker read
    exams._update(cfg.state_dir, "o/r", 1, state="open", reconciled=1, job=None)
    job = {"kind": "generate", "head_sha": "a", "attempt": 0, "started": NOW.isoformat()}
    assert exams._claim(cfg, stale, job, lambda: NOW) is False
    assert exams.read(cfg.state_dir, "o/r", 1)["job"] is None


class Tabs:
    def __init__(self, alive=None):
        self.alive, self.ran = alive, []

    def find(self, label):
        return SimpleNamespace(alive=True) if self.alive else None

    def ensure(self, workspace, label, cwd, env=None):
        self.label = label
        return SimpleNamespace(run=lambda command: self.ran.append(command) or True)


@pytest.fixture
def tabs(monkeypatch):
    def use(alive=None):
        fake = Tabs(alive)
        monkeypatch.setattr(exam_runner.herdr.Tab, "find", fake.find)
        monkeypatch.setattr(exam_runner.herdr.Tab, "ensure", fake.ensure)
        return fake
    return use


def test_the_pass_launches_the_runner_in_its_tab_and_never_runs_a_session(tmp_path, tabs, monkeypatch):
    monkeypatch.setattr(exams, "work", lambda *a, **kw: pytest.fail("the pass ran exam work"))
    cfg = make_config(tmp_path)
    fake = tabs()
    exam_runner.launch(cfg, "targets.yaml")
    assert fake.ran == []   # no exam: nothing to launch
    exams.request(cfg.state_dir, "o/r", 1)
    exam_runner.launch(cfg, "targets.yaml")
    [command] = fake.ran
    assert fake.label == "exam-runner"
    assert "-m dispatcher.main --config targets.yaml --exam-run; exit" in command
    busy = tabs(alive=True)
    exam_runner.launch(cfg, "targets.yaml")
    assert busy.ran == []


def test_a_second_runner_exits_without_working(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    ran = []
    monkeypatch.setattr(exam_runner, "tick", lambda *a: ran.append(1))
    monkeypatch.setattr(exam_runner, "fetch_all", lambda cfg: {})
    from dispatcher.state import state_lock
    with state_lock(cfg.state_dir, exam_runner.RUN_LOCK):
        exam_runner.run(cfg, None)
    assert ran == []
    exam_runner.run(cfg, None)
    assert ran == [1]


SCRIPT = Path(os.environ.get("PR_EXAM_SCRIPT", exam_runner.SCRIPT))


@pytest.mark.skipif(not SCRIPT.is_file(), reason="the pinned pr-exam skill is not installed")
@pytest.mark.parametrize("concepts", [[], ["retry-budget"]])
def test_the_pinned_validator_accepts_the_delta_the_host_requests(tmp_path, concepts):
    """The production adapter and the real script on the host's delta request."""
    from dispatcher.exams import _spec
    d = tmp_path / "exams" / REPO / "240"
    job = d / "job"
    job.mkdir(parents=True)
    diff = "diff --git a/r.py b/r.py\n+retry(budget=3)\n"
    (job / "diff.patch").write_text(diff)
    (job / "body.md").write_text("")
    (d / "passed.diff").write_text("diff --git a/l.py b/l.py\n+lease()\n")
    doc = {"repo": REPO, "pr": 240, "passed_concepts": ["lease-ownership"], "followup": None}
    spec = _spec(SimpleNamespace(state_dir=tmp_path), doc, "delta", NEW, job, None, lambda: NOW, None)
    (tmp_path / "exam-records" / REPO).mkdir(parents=True)
    questions = [{"id": f"q{i}", "concept": c, "type": "choice", "prompt": f"Why {c}?",
                  "options": ["one", "two"]} for i, c in enumerate(concepts)]
    exam = {"version": 1, "mode": "delta", "repo": REPO, "pr": 240, "head_sha": NEW,
            "concepts": [{"id": c, "title": c, "kind": "concept"} for c in concepts],
            "questions": questions,
            "key": {q["id"]: {"answer": 0, "explanation": "Because.", "hunk": "+retry(budget=3)"}
                    for q in questions}}
    out, request = job / "exam.json", job / "request.json"
    out.write_text(json.dumps(exam))
    request.write_text(json.dumps(spec))
    adapter = exam_runner.PrExam(SCRIPT)
    assert adapter.validate(out, request) == []
    stale = {**exam, "concepts": [{"id": "lease-ownership", "title": "l", "kind": "concept"}]}
    out.write_text(json.dumps(stale))
    assert any("lease-ownership" in e for e in adapter.validate(out, request))
