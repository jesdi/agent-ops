"""dispatcher/migrate.py: a task file in the shape before the stored status
reads as (stage, status) — the table of the scenario "migration command
rewrites each legacy shape" (#132)."""
import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from dispatcher import state
from dispatcher.migrate import MigrationError, legacy_status, migrate_state_dir
from dispatcher.state import Stage as S
from dispatcher.status import Kind, Status
from tests.statuses import PR, failed, human, waking

ANSWERS = {"kind": "answers", "path": "q.html", "fingerprint": "ab12"}
PLAN = {"kind": "plan-approval", "path": "review.html", "fingerprint": "cd34"}
REVIEW_PARK = Status(Kind.PARKED_REVIEW, path="review.html", fingerprint="cd34")
ANSWERS_PARK = Status(Kind.PARKED_ANSWERS, path="q.html", fingerprint="ab12")


def legacy(stage="implement", **kw) -> dict:
    return {"issue": 363, "repository": "portfolio_eval", "stage": stage, **kw}


MIGRATION = [
    (legacy(), S.IMPLEMENT, Status(Kind.RUNNING)),
    (legacy(park="parked", park_reason="decision", park_note="which DB?"),
     S.IMPLEMENT, human("decision", "which DB?")),
    (legacy(park="parked"), S.IMPLEMENT, human("operator")),
    (legacy("queued", park="parked", park_reason="access"), S.QUEUED, Status(Kind.PARKED_ACCESS)),
    (legacy("spec", park="parked", operator_request=ANSWERS), S.SPEC, ANSWERS_PARK),
    (legacy("awaiting-plan-review", park="awaiting-review", operator_request=PLAN),
     S.PLAN, REVIEW_PARK),
    (legacy("awaiting-plan-review", operator_request=PLAN), S.PLAN,
     Status(Kind.HELD_PLAN_APPROVAL, path="review.html", fingerprint="cd34")),
    (legacy("awaiting-plan-review"), S.PLAN, Status(Kind.RUNNING)),
    (legacy(park="awaiting-ci", ci_run_id=991), S.IMPLEMENT, Status(Kind.PARKED_CI, run_id=991)),
    (legacy("spec", park="parked-login"), S.SPEC, Status(Kind.HELD_LOGIN)),
    (legacy("plan", park="unpark-requested"), S.PLAN, waking(human("operator"), "reply")),
    (legacy(park="unpark-requested", crashed_stage="implement"), S.IMPLEMENT,
     waking(failed("session-crash"), "resume")),
    (legacy("spec", park="unpark-requested", operator_request=ANSWERS), S.SPEC,
     waking(ANSWERS_PARK, "reply")),
    (legacy("awaiting-plan-review", park="unpark-requested", operator_request=PLAN),
     S.PLAN, waking(REVIEW_PARK, "reply")),
    (legacy("queued", park="unpark-requested", park_reason="access"), S.QUEUED,
     waking(Status(Kind.PARKED_ACCESS), "access")),
    (legacy("queued", unlaunched=True), S.QUEUED, waking(None, "launch")),
    (legacy(implement_providers=["anthropic"]), S.REVIEW, waking(None, "next-stage")),
    (legacy(implement_providers=["anthropic"], picks={"implement": "anthropic/x"}),
     S.IMPLEMENT, Status(Kind.RUNNING)),
    (legacy("pr-open", feedback_pending=True, attention="conflict"), S.PR_OPEN,
     waking(PR, "attention(conflict)")),
    (legacy("pr-open"), S.PR_OPEN, PR),
    (legacy("pr-open", park="parked", park_reason="loop cap"), S.PR_OPEN, human("loop-cap")),
    (legacy("failed", crashed_stage="implement"), S.IMPLEMENT, failed("session-crash")),
    (legacy("failed", park="parked", pr_number=88), S.PR_OPEN, failed("unknown")),
    (legacy("failed", pr_number=88), S.PR_OPEN, failed("unknown")),
    (legacy("failed"), S.QUEUED, failed("unknown")),
    (legacy("done", pr_number=88), S.PR_OPEN, Status(Kind.DONE)),
    (legacy("canceled"), S.QUEUED, Status(Kind.CANCELED)),
    (legacy("blocked"), S.IMPLEMENT, human("operator", "migrated from legacy blocked stage")),
    (legacy("stalled-on-budget"), S.SPEC,
     human("operator", "migrated from legacy stalled-on-budget stage")),
]


@pytest.mark.parametrize("shape, stage, status", MIGRATION)
def test_each_legacy_shape_reads_as_the_migration_table_names(shape, stage, status):
    assert legacy_status(shape) == (stage, status)


@pytest.mark.parametrize("shape", [
    legacy(park="awaiting-ci", ci_run_id=0),
    legacy(park="parked", park_reason="no such reason"),
    legacy("awaiting-plan-review", operator_request=ANSWERS),
    legacy("spec", park="parked", operator_request={"kind": "nope", "path": "x"}),
    legacy("no-such-stage"),
])
def test_a_shape_the_table_does_not_classify_is_refused(shape):
    with pytest.raises(ValueError):
        legacy_status(shape)


# ---- migrate_state_dir: the files of a state dir rewritten -----------------

BOX_TASKS = Path(__file__).resolve().parents[1] / ".scratch" / "box-tasks"
# The legacy keys the status replaced. park_msg_id and hold_for_attach are
# still TaskState fields until inbound Telegram goes (ticket 12): a parked
# task keeps the message a Telegram reply is routed by.
_RETIRED_KEYS = ("park", "park_reason", "park_note", "feedback_pending", "crashed_stage",
                 "unlaunched", "operator_request")


def legacy_file(stage="implement", issue=363, **kw) -> dict:
    """A whole task file as the dispatcher wrote it before the status."""
    d = asdict(state.TaskState(issue=issue, repository="portfolio_eval", stage=S.SPEC,
                               slot=0, worktree="/w", branch="agent/task", title="t",
                               updated_at="2026-11-01T00:00:00+00:00"))
    del d["status"], d["plan_approval"]
    d.update({"stage": stage, "park": "", "park_reason": "", "park_note": "", "ci_run_id": 0,
              "crashed_stage": "", "unlaunched": False, "operator_request": None,
              "feedback_pending": False, **kw})
    return d


def write(state_dir: Path, d: dict, name: str | None = None) -> Path:
    p = state_dir / (name or f"task-{d.get('repository', d.get('target'))}-{d['issue']}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))
    return p


def contents(state_dir: Path) -> dict[str, bytes]:
    return {str(p.relative_to(state_dir)): p.read_bytes()
            for p in sorted(state_dir.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("shape, stage, status", MIGRATION)
def test_migration_rewrites_each_legacy_shape_to_the_table(tmp_path, shape, stage, status):
    write(tmp_path, legacy_file(**shape))
    migrate_state_dir(tmp_path)
    task = state.load(tmp_path, "portfolio_eval", 363)
    assert (task.stage, task.status) == (stage, status)


def test_a_migrated_file_has_only_the_new_shape(tmp_path):
    p = write(tmp_path, legacy_file(park="parked", park_reason="decision", park_note="n",
                                    park_msg_id=12, hold_for_attach=True))
    assert migrate_state_dir(tmp_path) == [p]
    d = json.loads(p.read_text())
    assert {"stage", "status", "plan_approval"} <= d.keys()
    assert not d.keys() & set(_RETIRED_KEYS)
    assert (d["plan_approval"], d["park_msg_id"]) == ("", 12)


@pytest.mark.parametrize("bad", ["{not json", json.dumps(legacy_file(park="awaiting-ci"))])
def test_one_bad_file_names_it_and_changes_no_file(tmp_path, bad):
    for issue in range(1, 13):
        write(tmp_path, legacy_file(issue=issue))
    broken = tmp_path / "task-portfolio_eval-99.json"
    broken.write_text(bad)
    before = contents(tmp_path)
    with pytest.raises(MigrationError, match="task-portfolio_eval-99.json") as err:
        migrate_state_dir(tmp_path)
    assert err.value.path == broken
    assert contents(tmp_path) == before


def test_a_second_run_changes_no_byte(tmp_path):
    write(tmp_path, legacy_file(park="parked"))
    write(tmp_path, legacy_file("done", issue=7, pr_number=88))
    migrate_state_dir(tmp_path)
    before = contents(tmp_path)
    assert migrate_state_dir(tmp_path) == []
    assert contents(tmp_path) == before


def test_a_run_killed_midway_is_completed_by_the_next(tmp_path):
    files = [legacy_file(park="parked", issue=1), legacy_file("failed", issue=2),
             legacy_file("queued", issue=3, unlaunched=True)]
    whole, killed = tmp_path / "whole", tmp_path / "killed"
    for d in files:
        write(whole, d)
        write(killed, d)
    migrate_state_dir(whole)
    shutil.copy(whole / "task-portfolio_eval-2.json", killed / "task-portfolio_eval-2.json")
    migrate_state_dir(killed)
    assert contents(killed) == contents(whole)


def test_a_request_with_no_fingerprint_is_never_approvable(tmp_path):
    write(tmp_path, legacy_file("awaiting-plan-review",
                                operator_request={"kind": "plan-approval", "path": "r.html"}))
    migrate_state_dir(tmp_path)
    task = state.load(tmp_path, "portfolio_eval", 363)
    assert (task.status.kind, task.status.fingerprint, task.plan_approval) == \
        (Kind.HELD_PLAN_APPROVAL, "", "")


def test_a_legacy_twin_is_rewritten_in_place(tmp_path):
    """Today's loader reads `task-<issue>.json` for its own repository only
    when the keyed file is absent; moving it could overwrite that file."""
    twin = write(tmp_path, legacy_file(park="parked"), name="task-363.json")
    migrate_state_dir(tmp_path)
    assert [p.name for p in tmp_path.glob("task-*")] == ["task-363.json"]
    assert state.load(tmp_path, "portfolio_eval", 363).status == human("operator")
    assert "status" in json.loads(twin.read_text())


def test_a_file_with_target_is_rewritten_with_repository(tmp_path):
    d = legacy_file()
    d["target"] = d.pop("repository")
    p = write(tmp_path, d)
    migrate_state_dir(tmp_path)
    assert json.loads(p.read_text())["repository"] == "portfolio_eval"


def test_an_archived_snapshot_is_rewritten_and_still_loads(tmp_path):
    root = state.archive_root(tmp_path, "portfolio_eval", 363)
    write(root, legacy_file("done", pr_number=88), name="task.json")
    assert migrate_state_dir(tmp_path) == [root / "task.json"]
    archived = state.load_archived(tmp_path, "portfolio_eval", 363)
    assert (archived.stage, archived.status) == (S.PR_OPEN, Status(Kind.DONE))


def test_the_console_page_of_a_migrated_finished_task_loads(tmp_path):
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS, FakeSources, make_config, make_repository
    from web.app import create_app
    root = state.archive_root(tmp_path, "portfolio_eval", 363)
    write(root, legacy_file("done", pr_number=88), name="task.json")
    migrate_state_dir(tmp_path)
    app = create_app(make_config(tmp_path, [make_repository("portfolio_eval", "jesdi/pe")]),
                     FakeSources(tmp_path))
    response = TestClient(app).get("/api/task/portfolio_eval/363", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["card"]["issue"] == 363


@pytest.mark.skipif(not BOX_TASKS.is_dir(), reason="no box task files copied")
def test_every_box_task_file_is_classified(tmp_path):
    for p in BOX_TASKS.glob("task-*.json"):
        shutil.copy(p, tmp_path / p.name)
    migrated = migrate_state_dir(tmp_path)
    tasks, unreadable = state.scan(tmp_path)
    files = len(list(BOX_TASKS.glob("task-*.json")))
    assert (len(migrated), len(tasks), unreadable) == (files, files, [])
