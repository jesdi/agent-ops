"""Acceptance tests for ticket 08: the one-time migration of old-flow task files.

Spec: specs/2026-10-06-openspec-pipeline/spec.md, requirements 21 and 22 and the
scenarios "an old task with an unanswered questionnaire restarts", "an old task at
the spec review gate restarts from its design", "a task with an open pull request
is untouched". Black-box: the command runs as a subprocess,
`python -m dispatcher.openspec_migration <state_dir>`, on RAW JSON task files of
the old flow (the new loader rejects the old stage and request kind). What the next
pass does is observed through dispatcher.main.run_pass over the fakes of
tests/test_main.py. Every state directory is a pytest tmp_path.

Not asserted on purpose: whether spec-stage tasks are still migrated when an
undrained task (plan, implement, review) is present; the exact fields the migration
writes; whether the old design file is deleted by the migration (the spec session
does that); convergence.lock content or mtime (taking the lock may touch it).
"""
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher.convergence import pass_lock
from dispatcher.state import Stage, TaskState, load

from tests.test_main import (FakeSessions, cfg, deps, patch_usage,
                             patch_workspace)
from tests.test_spec_stage_without_gate_acceptance import _spec_prompt

ROOT = Path(__file__).resolve().parent.parent
TARGET = "portfolio_eval"
QUESTIONNAIRE = ".agent/questionnaire.md"
DESIGN = "docs/specs/2026-10-01-alerts-design.md"
NEW_ONLY = ("plan_slips", "unattended_rounds", "gated", "asked")


def run_cmd(state_dir, timeout=20):
    """The command, as the operator runs it. A hang fails the test."""
    try:
        return subprocess.run(
            [sys.executable, "-m", "dispatcher.openspec_migration", str(state_dir)],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail("the command blocked instead of finishing or refusing")


def out(r):
    return r.stdout + r.stderr


def snapshot(state_dir):
    """Bytes and mtime of every file, except the lock file a pass lock may touch."""
    return {str(p.relative_to(state_dir)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(Path(state_dir).rglob("*"))
            if p.is_file() and p.name != "convergence.lock"}


def task_file(c, issue):
    return Path(c.state_dir) / f"task-{TARGET}-{issue}.json"


def old_task(c, issue, stage, **raw):
    """Write the task file as the pre-migration code wrote it: raw JSON, the old
    stage and request kinds, `ticket_cursor`, none of the new fields. Returns the
    worktree."""
    wt = Path(c.targets[0].worktrees_path) / f"task-{issue}"
    (wt / ".agent").mkdir(parents=True, exist_ok=True)
    d = asdict(TaskState(issue=issue, target=TARGET, stage=Stage.QUEUED, slot=0,
                         worktree=str(wt), branch=f"agent/task-{issue}",
                         title="old task", updated_at="2026-09-20T00:00:00+00:00",
                         track="standard"))
    for k in NEW_ONLY:
        d.pop(k)
    d["stage"] = stage
    d["ticket_cursor"] = 0
    d.update(raw)
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    task_file(c, issue).write_text(json.dumps(d, indent=2))
    return wt


def parked_spec(c, issue=370):
    wt = old_task(c, issue, "spec", park="parked", park_note="5 questions",
                  park_msg_id=11,
                  operator_request={"kind": "answers", "path": QUESTIONNAIRE})
    (wt / QUESTIONNAIRE).write_text(
        "# Questions\n" + "".join(f"{i}. Open question {i}?\n" for i in range(1, 6)))
    return wt


def gated_spec(c, issue=384):
    wt = old_task(c, issue, "awaiting-spec-review", park="awaiting-review",
                  spec_path=DESIGN, operator_request={"kind": "spec-approval"})
    (wt / DESIGN).parent.mkdir(parents=True)
    (wt / DESIGN).write_text("# Alerts design\n" + "decision " * 200)
    return wt


def pr_open(c, issue=391):
    return old_task(c, issue, "pr-open", pr_number=77, ticket_cursor=3,
                    spec_path=DESIGN)


def cfg_(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return cfg(tmp_path)


def next_pass(c):
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    return sess


def migrate_ok(c):
    r = run_cmd(c.state_dir)
    assert r.returncode == 0, out(r)
    return r


# 1. An old task with an unanswered questionnaire restarts.
def test_unanswered_questionnaire_task_restarts_with_a_fresh_spec_session(
        tmp_path, monkeypatch):
    c = cfg_(tmp_path, monkeypatch)
    wt = parked_spec(c)
    questions = (wt / QUESTIONNAIRE).read_bytes()

    migrate_ok(c)

    assert load(c.state_dir, TARGET, 370).asked is True
    sess = next_pass(c)
    assert [s[:2] for s in sess.spawned] == [(370, "spec")]
    assert sess.resumed == []
    assert (wt / QUESTIONNAIRE).read_bytes() == questions


# 2. An old task at the spec review gate restarts from its design.
def test_old_gate_task_loads_and_restarts_with_a_fresh_spec_session(
        tmp_path, monkeypatch):
    c = cfg_(tmp_path, monkeypatch)
    wt = gated_spec(c)
    design = (wt / DESIGN).read_bytes()
    with pytest.raises(ValueError):          # the migration is needed
        load(c.state_dir, TARGET, 384)

    migrate_ok(c)

    t = load(c.state_dir, TARGET, 384)
    assert t.stage not in (Stage.AWAITING_PLAN_REVIEW,)
    assert t.operator_request is None or t.operator_request.kind != "spec-approval"
    assert t.park != "awaiting-review"
    sess = next_pass(c)
    assert [s[:2] for s in sess.spawned] == [(384, "spec")]
    assert sess.resumed == []
    assert (wt / DESIGN).read_bytes() == design   # input for the new session


# 3. A task with an open pull request is untouched.
def test_pr_open_task_is_untouched_and_starts_no_session(tmp_path, monkeypatch):
    c = cfg_(tmp_path, monkeypatch)
    pr_open(c)
    gated_spec(c)                      # something to migrate, so the command acts
    before = task_file(c, 391).read_bytes()

    migrate_ok(c)

    assert json.loads(task_file(c, 384).read_text())["stage"] != "awaiting-spec-review"
    assert task_file(c, 391).read_bytes() == before
    t = load(c.state_dir, TARGET, 391)
    assert t.stage is Stage.PR_OPEN and t.pr_number == 77
    sess = next_pass(c)
    assert [s for s in sess.spawned if s[0] == 391] == []
    assert [r for r in sess.resumed if r[0] == 391] == []


# 4. A task in plan, implement or review is listed and left alone.
@pytest.mark.parametrize("stage", ["plan", "implement", "review"])
def test_task_in_plan_implement_or_review_is_reported_not_changed(
        tmp_path, monkeypatch, stage):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 377, stage)
    before = task_file(c, 377).read_bytes()

    r = run_cmd(c.state_dir)

    assert task_file(c, 377).read_bytes() == before
    assert r.returncode != 0
    assert "drain" in out(r).lower(), out(r)
    assert any(TARGET in ln and "377" in ln for ln in out(r).splitlines()), out(r)


# 5. A second run changes no file.
def test_second_run_changes_no_file(tmp_path, monkeypatch):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    pr_open(c)
    migrate_ok(c)
    assert json.loads(task_file(c, 384).read_text())["stage"] != "awaiting-spec-review"
    after_first = snapshot(c.state_dir)

    migrate_ok(c)

    assert snapshot(c.state_dir) == after_first


# 6. The command refuses to run while a pass holds the pass lock.
def test_command_refuses_while_a_pass_holds_the_lock(tmp_path, monkeypatch):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    with pass_lock(c.state_dir):
        before = snapshot(c.state_dir)
        r = run_cmd(c.state_dir)
        after = snapshot(c.state_dir)
    assert r.returncode != 0
    assert any(w in out(r).lower() for w in ("lock", "dispatcher")), out(r)
    assert "No module named" not in r.stderr
    assert after == before
    assert json.loads(task_file(c, 384).read_text())["stage"] == "awaiting-spec-review"


# 7. The spec prompt tells the session what to do with what the task already has.
def _chunks(prompt):
    """Paragraphs and single list items of the prompt."""
    items = []
    for para in prompt.split("\n\n"):
        items.extend(para.split("\n- ") if "\n- " in para else [para])
    return items


def _rule(prompt, *needles):
    return [ch for ch in _chunks(prompt) if all(n in ch for n in needles)]


def test_spec_prompt_names_the_restart_inputs(tmp_path, monkeypatch):
    p = _spec_prompt(tmp_path, monkeypatch, ("auto",))
    assert _rule(p, "unanswered", "questionnaire.html", "again", "as they are"), (
        "no one paragraph tells a session to ask an unanswered questionnaire again as it is")
    settled = _rule(p, "answered", "questionnaire.html", "settled")
    assert any(w in ch for ch in settled for w in ("no new", "raise no", "not raise")), (
        "no one paragraph takes answers as settled and raises no new questionnaire")
    assert _rule(p, "-design.md", "spec-ready", "remove"), (
        "no one paragraph handles an old docs/specs design file as a spec-ready body "
        "and removes it")


# Extras.
def test_empty_state_dir_exits_zero_and_creates_nothing(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    r = run_cmd(state)
    assert r.returncode == 0, out(r)
    assert "No module named" not in r.stderr
    assert [p.name for p in state.iterdir() if p.name != "convergence.lock"] == []


def test_missing_state_dir_fails_and_creates_nothing(tmp_path):
    state = tmp_path / "nope"
    r = run_cmd(state)
    assert r.returncode != 0
    assert "No module named" not in r.stderr
    assert str(state) in out(r) or "exist" in out(r).lower(), out(r)
    assert not state.exists()


def test_corrupt_task_file_does_not_leave_others_half_migrated(tmp_path, monkeypatch):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    bad = task_file(c, 999)
    bad.write_text('{"issue": 999, "stage": "sp')
    before = bad.read_bytes()

    r = run_cmd(c.state_dir)

    assert "No module named" not in r.stderr
    assert r.returncode != 0 and "999" in out(r), out(r)
    assert bad.read_bytes() == before
    for p in Path(c.state_dir).glob("task-*.json"):
        if p != bad:
            json.loads(p.read_text())         # not truncated, not invalid
    assert not list(Path(c.state_dir).glob("*.tmp"))
    assert not list(Path(c.state_dir).glob(".*.tmp"))
