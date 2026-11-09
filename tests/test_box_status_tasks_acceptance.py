"""Acceptance (ticket 03): the `tasks` and `questions` rows of `agent-ops status`,
robust to a herdr socket that is gone and to corrupt or vanished task files."""
import io
from datetime import timedelta
from pathlib import Path

import pytest

from dispatcher import state
from dispatcher.state import (PARK_HUMAN, PARK_CI, PARK_REVIEW, WHY_DECISION,
                              WHY_STALLED, AnswersRequest, PlanApprovalRequest, Stage)
from tests.test_box_status_acceptance import NOW, Box, verdict
from web import box_status
from web.box_status import Probes

URL = "https://box.example.ts.net"
NOTE = "Which currency should the invoice default to when the customer has none set?"


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def section(text, label):
    """The row `label` plus any indented continuation lines under it."""
    lines, found = text.splitlines()[1:], []
    for i, l in enumerate(lines):
        if l.split(None, 1)[:1] == [label]:
            found = [l]
            for more in lines[i + 1:]:
                if not more.startswith(" "):
                    break
                found.append(more)
            break
    assert found, f"no {label!r} row in:\n{text}"
    return "\n".join(found)


def console(box, url=URL):
    box.config.write_text(box.config.read_text() + f"console_url: '{url}'\n")


def plan_request(box, issue=281):
    box.task(issue, stage=Stage.AWAITING_PLAN_REVIEW, park=PARK_REVIEW,
             operator_request=PlanApprovalRequest(path=".agent/review.html"))


def test_tasks_row_lists_parked_and_live_tasks_and_slots_count(box):
    box.task(270, stage=Stage.REVIEW, park=PARK_CI)
    box.task(281)
    box.tabs = {"task-agent-ops-281"}
    code, text = box.report()
    tasks = section(text, "tasks")
    assert "agent-ops#270  review  awaiting-ci  2h" in tasks
    assert "agent-ops#281  implement  live  task-agent-ops-281" in tasks
    assert "1/3" in section(text, "slots")


def test_legacy_task_file_is_still_listed(box):
    box.task(300)
    (box.state / "task-agent-ops-300.json").rename(box.state / "task-300.json")
    assert "agent-ops#300" in section(box.report()[1], "tasks")


def test_questions_row_lists_plan_approval_and_decision_with_console_links(box):
    console(box)
    plan_request(box)
    box.task(12, repository="billing", park=PARK_HUMAN, park_reason=WHY_DECISION,
             park_note=NOTE, updated_at="2026-11-06T08:00:00+00:00")
    questions = section(box.report()[1], "questions")
    assert ("agent-ops#281  plan approval  .agent/review.html  2h  "
            f"{URL}/task/agent-ops/281/review") in questions
    assert ('billing#12  decision  "Which currency should the invoice default '
            f'to when the custo…"  4h  {URL}/task/billing/12') in questions


def test_answers_request_is_listed_as_answers(box):
    box.task(31, stage=Stage.SPEC, park=PARK_HUMAN, park_reason=WHY_DECISION,
             operator_request=AnswersRequest(path=".agent/questions.html"))
    assert "agent-ops#31  answers  .agent/questions.html  2h" in section(
        box.report()[1], "questions")


def test_questions_without_console_url_have_no_link(box):
    plan_request(box)
    questions = section(box.report()[1], "questions")
    assert "agent-ops#281  plan approval  .agent/review.html  2h" in questions
    assert "http" not in questions and "/task/" not in questions


def test_repository_with_a_dot_round_trips_into_the_console_link(box):
    console(box)
    box.task(12, repository="my.repo", park=PARK_HUMAN, park_reason=WHY_DECISION,
             park_note="ok?")
    assert f"{URL}/task/my.repo/12" in section(box.report()[1], "questions")


def test_stalled_human_park_is_a_task_not_a_question(box):
    box.task(55, park=PARK_HUMAN, park_reason=WHY_STALLED)
    code, text = box.report()
    assert "agent-ops#55" in section(text, "tasks")
    assert "agent-ops#55" not in section(text, "questions")


def test_corrupt_task_file_shows_as_unknown_and_exit_is_zero(box):
    (box.state / "task-agent-ops-299.json").write_text("{not json")
    code, text = box.report()
    assert "agent-ops#299  unknown (unreadable task file)" in section(text, "tasks")
    assert code == 0


def test_missing_herdr_socket_degrades_liveness_not_the_report(box):
    box.task(281)

    def no_herdr():
        raise RuntimeError("herdr socket missing")
    out = io.StringIO()
    code = box_status.run(str(box.config), NOW,
                          Probes(units=lambda: dict(box.units), tabs=no_herdr), out)
    text = out.getvalue()
    assert "unknown (herdr socket missing)" in section(text, "tasks")
    assert "herdr" in verdict(text)
    for label in ("dispatcher", "units", "projects", "holds", "slots", "questions"):
        section(text, label)
    assert code == 0


def test_load_all_survives_a_task_file_deleted_between_listing_and_reading(
        tmp_path, monkeypatch):
    for issue in (1, 2, 3):
        state.save(tmp_path, _task(issue))
    victim = tmp_path / "task-agent-ops-2.json"
    real = Path.read_text

    def vanishing(self, *a, **k):
        if self == victim:
            self.unlink()
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", vanishing)
    assert [t.issue for t in state.load_all(tmp_path)] == [1, 3]


def _task(issue):
    from tests.webfakes import make_task
    return make_task(issue=issue, repository="agent-ops")
