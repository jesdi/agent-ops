"""Acceptance tests for ticket 02: the spec session writes stage 1 and hands
over to plan, with no spec approval gate.

Spec: specs/2026-10-06-openspec-pipeline/spec.md, requirements 1-4 and the
scenarios "the spec session ends without a review gate", "a spec session that
names no configured track is bounced", "an open decision raises one
questionnaire", "a spec-ready issue is not interviewed", "a bug is reproduced
before its spec is written". Black-box, through dispatcher.main.run_pass over
the fakes of tests/test_main.py.
"""
import json
import re
from dataclasses import replace as dc_replace
from datetime import date

import pytest

import dispatcher.main as main
from dispatcher import intents as intents_mod
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import PARK_HUMAN, Stage, load

from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, cfg,
                             deps, make_task, patch_usage, patch_workspace)

FOLDER = "specs/2026-10-12-csv-export"
SPEC = f"{FOLDER}/spec.md"


def _track(name):
    m = ["claude-opus-5"]
    return {"when": f"The {name} track.", "spec": m, "plan": m,
            "implement": m, "review": m}


def _cfg(tmp_path):
    """The spec's tracks: security, standard, trivial."""
    policy = parse_policy({
        "triage": ["claude-sonnet-5@low"], "untracked": "standard",
        "tracks": {n: _track(n) for n in ("security", "standard", "trivial")},
    })
    return dc_replace(cfg(tmp_path), models=policy)


def _write_stage1(wt, body_ok=True):
    folder = wt / FOLDER
    folder.mkdir(parents=True)
    (folder / "proposal.md").write_text("# Proposal\n\n## Why\n\n" + "w " * 400)
    if body_ok:
        (folder / "spec.md").write_text(
            "# Export a portfolio as CSV: Spec\n\n## Requirements\n\n"
            + "x " * 400 + "\n\n## Scenarios\n\n" + "y " * 400)


def _signal(wt, **kw):
    (wt / ".agent" / "stage.json").write_text(json.dumps(kw))


def _done(wt, track="standard", artifact=SPEC):
    _signal(wt, stage="spec", status="done", artifact=artifact, track=track,
            note="stage 1 written")


def _setup(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return _cfg(tmp_path)


def _blocks(p):
    """Paragraphs and list items of a prompt."""
    import re
    return [b for para in p.split("\n\n") for b in re.split(r"\n(?=\s*[-*] )", para)]


def _assert_no_write_under_docs_specs(p):
    """No block naming docs/specs holds a write-class verb. A removal rule
    passes: its only "adds" is the phrase "the commit that adds the spec
    folder", deleted before matching; "added" (old files) is no instruction."""
    import re
    verbs = re.compile(r"\b(write|create|save|commit|put|add|store|place)\b")
    for b in _blocks(p):
        b = b.lower().replace("in the commit that adds", "")
        b = b.replace("the commit that adds", "")
        assert not ("docs/specs" in b and verbs.search(b)), b


def _spec_prompt(tmp_path, monkeypatch, labels):
    c = _setup(tmp_path, monkeypatch)
    gh = FakeGitHub([Candidate(42, "Export a portfolio as CSV", "u42",
                               labels=labels)])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    (issue, stage, _model, prompt, _effort) = sess.spawned[0]
    assert (issue, stage) == (42, "spec")
    return prompt.lower()


# 1. The spec session ends without a review gate.
def test_spec_session_ends_without_a_review_gate(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard")
    _write_stage1(wt)
    _done(wt)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)

    main.run_pass(c, d)

    assert [s[:2] for s in sess.spawned] == [(42, "plan")]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.PLAN
    assert t.park == ""
    assert not any("review" in n for n in d.notifier.sent)
    assert (t.operator_request is None
            or t.operator_request.kind != "plan-approval")
    assert t.stage.value != "awaiting-plan-review"


# 2. A track that is not configured is bounced once.
def test_spec_session_naming_an_unconfigured_track_is_bounced(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard",
                   picks={"spec": "anthropic/claude-opus-5"})
    _write_stage1(wt)
    _done(wt, track="fast")
    sess = FakeSessions(alive={42})

    main.run_pass(c, deps(sess=sess))

    (resume,) = sess.resumed
    assert "security, standard, trivial" in resume[1].replace("'", "")
    assert [s for s in sess.spawned if s[1] == "plan"] == []
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.SPEC and t.park == ""


# 3. A done report with a bad spec.md fails the task.
@pytest.mark.parametrize("make_spec", [False, True], ids=["missing", "too-small"])
def test_done_report_with_a_bad_spec_md_fails_the_task(tmp_path, monkeypatch, make_spec):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard")
    _write_stage1(wt, body_ok=False)
    if make_spec:
        (wt / SPEC).write_text("# tiny\n")
    _done(wt)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)

    main.run_pass(c, d)

    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.FAILED
    assert [s for s in sess.spawned if s[1] == "plan"] == []
    (ctx,) = [x for t, x in d.notifier.contexts if t == "artifact_failed"]
    assert "spec.md" in json.dumps(ctx)


# 4. An open decision raises one questionnaire.
def test_default_spec_prompt_asks_one_questionnaire_before_writing(tmp_path, monkeypatch):
    p = _spec_prompt(tmp_path, monkeypatch, ("auto",))
    assert "questionnaire" in p
    assert "one questionnaire" in p
    assert "every open decision" in p
    assert "awaiting-answers" in p
    assert "nothing under" in p and "specs/" in p


# 5. A spec-ready issue is not interviewed.
def test_spec_ready_prompt_has_no_interview_and_keeps_decisions_verbatim(tmp_path, monkeypatch):
    p = _spec_prompt(tmp_path, monkeypatch, ("auto", "spec-ready"))
    assert "spec-ready" in p
    assert "word for word" in p
    assert "stage 1" in p
    assert "to-openspec" in p
    assert "contradict" in p
    assert "raise no questionnaire" in p
    # Requirement 3, in the ONE paragraph that is the spec-ready case.
    (para,) = [" ".join(b.split()) for b in p.split("\n\n")
               if b.startswith("**`spec-ready`**")]
    assert "raise no questionnaire" in para and "word for word" in para
    assert "record in `spec.md` what moved in the code" in para
    assert "how the design absorbs it" in para
    exception = para[para.index("the one exception"):]
    assert "contradicts a settled decision" in exception
    assert "questionnaire" in exception
    assert "about that one decision only" in exception
    assert "recommended answer" in exception
    assert re.search(r"never[^.]*the other settled decisions", exception)


# 6. A bug is reproduced before its spec is written.
def test_bug_prompt_reproduces_first_and_writes_no_diagnosis_document(tmp_path, monkeypatch):
    p = _spec_prompt(tmp_path, monkeypatch, ("auto", "bug"))
    assert "failing end-to-end test" in p
    assert "root cause" in p
    assert "why" in p and "proposal.md" in p
    assert "first scenario" in p
    _assert_no_write_under_docs_specs(p)
    for b in _blocks(p):
        assert "-diagnosis.md" not in b and "write a diagnosis" not in b, b
    assert "to-openspec" in p and "stage 1" in p


# 7. The folder and the stage-1-only rule.
def test_spec_prompt_names_the_dated_folder_and_stage_1_only(tmp_path, monkeypatch):
    p = _spec_prompt(tmp_path, monkeypatch, ("auto",))
    assert f"specs/{date.today().isoformat()}-<topic>/" in p or "specs/<today" in p
    assert "to-openspec" in p
    assert "stage 1" in p
    assert "stage 2" in p            # named, to say it is not run here
    assert "proposal.md" in p and "spec.md" in p
    _assert_no_write_under_docs_specs(p)


# 8. The dispatcher records that a spec session asked a questionnaire.
def _ask(c, wt):
    (wt / ".agent" / "questionnaire.md").write_text("# Q\n")
    _signal(wt, stage="spec", status="awaiting-answers", note="4 questions",
            artifact=".agent/questionnaire.md")
    main.run_pass(c, deps(sess=FakeSessions(alive={42}), notifier=FakeNotifier()))


def test_parking_for_answers_records_a_questionnaire_and_survives_a_fresh_session(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard")
    _ask(c, wt)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.park == PARK_HUMAN
    assert t.asked is True

    # The operator answers; the session dies; the operator resumes: a fresh
    # spec session replaces the old one.
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions()))
    main.run_pass(c, deps(sess=FakeSessions(alive=set())))
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.FAILED
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 2)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [s[:2] for s in sess.spawned] == [(42, "spec")]
    assert load(c.state_dir, "portfolio_eval", 42).asked is True


def test_a_spec_session_that_never_parked_is_not_recorded_as_asking(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard")
    _write_stage1(wt)
    _done(wt)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.PLAN
    assert t.asked is False
