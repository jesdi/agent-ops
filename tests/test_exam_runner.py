"""The production edges of exam generation: the claim race, the session's container
and the status token. The seam's own behaviour is in test_exam_generation.py."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from dispatcher import containers, exam_runner, exams
from tests.webfakes import make_config

NOW = datetime(2026, 11, 19, 23, 59, tzinfo=timezone.utc)


def test_the_exam_container_has_no_token_and_no_clone(monkeypatch):
    cmd = containers.exam_cmd("exam-x", "/job", "/rec", "/skill-dir", "1g", "1",
                              "anthropic/claude-opus-5-5", "/job/prompt.md")
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert "/job:/job" in mounts and "/rec:/rec:ro" in mounts and "/skill-dir:/skill:ro" in mounts
    assert not any("/root/.config/gh" in m or ".gitconfig" in m for m in mounts)


def test_the_codex_exam_session_runs_outside_a_git_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    cmd = containers.exam_cmd("exam-x", "/job", "/rec", "/skill-dir", "1g", "1",
                              "openai/gpt-5", "/job/prompt.md")
    assert cmd[-1].startswith("codex exec ") and "--skip-git-repo-check" in cmd[-1]


def test_only_the_exam_token_holds_statuses_write(tmp_path):
    asked = []
    access = SimpleNamespace(grants=lambda repos, perms: asked.append(perms)
                             or {repos[0].lower(): SimpleNamespace(token="T")})
    calls = []
    run = lambda argv, **kw: calls.append(kw["env"]["GH_TOKEN"]) or SimpleNamespace(stdout="")
    exam_runner.Github(access, None, run=run).post_status("O/r", "abc", "exam", "pending", "x")
    assert asked == [exam_runner.PERMISSIONS] and calls == ["T"]
    assert exam_runner.PERMISSIONS["statuses"] == "write"


def _github(run):
    access = SimpleNamespace(grants=lambda repos, perms: {repos[0].lower(): SimpleNamespace(token="T")})
    return exam_runner.Github(access, None, run=run)


def _gh_fails(stderr):
    def run(argv, **kw):
        raise subprocess.CalledProcessError(1, argv, stderr=stderr)
    return run


def test_a_missing_glossary_reads_as_none_and_other_failures_raise():
    assert _github(lambda argv, **kw: SimpleNamespace(stdout="terms")).file("o/r", "main", "GLOSSARY.md") == "terms"
    assert _github(_gh_fails("HTTP 404: Not Found")).file("o/r", "main", "GLOSSARY.md") is None
    try:
        _github(_gh_fails("HTTP 500")).file("o/r", "main", "GLOSSARY.md")
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("a 500 must not read as a missing glossary")


def _script(returncode, stdout="", stderr=""):
    calls = []
    def run(argv, **kw):
        calls.append(argv[3:])
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return exam_runner.PrExam(Path("/s/pr_exam.py"), run=run), calls


def _raises(fn, message):
    try:
        fn()
    except ValueError as exc:
        assert str(exc) == message
    else:
        raise AssertionError(f"expected ValueError({message!r})")


def test_validate_returns_the_scripts_errors():
    ok, calls = _script(0)
    assert ok.validate(Path("e.json"), Path("r.json")) == []
    assert calls == [["validate", "--request", "r.json", "e.json"]]
    assert _script(1, stderr="a\nb")[0].validate(Path("e"), Path("r")) == ["a", "b"]
    assert _script(1)[0].validate(Path("e"), Path("r")) == ["validate failed"]


def test_public_parses_the_projection_and_raises_on_failure():
    assert _script(0, stdout='{"questions": []}')[0].public(Path("e")) == {"questions": []}
    _raises(lambda: _script(1, stderr=" bad \n")[0].public(Path("e")), "bad")
    _raises(lambda: _script(1)[0].public(Path("e")), "public failed")


def test_score_and_record_raise_on_a_script_failure():
    assert _script(0, stdout='{"passed": true}')[0].score(Path("e"), Path("a"), Path("g")) == {"passed": True}
    _raises(lambda: _script(1)[0].score(Path("e"), Path("a"), Path("g")), "score failed")
    ok, calls = _script(0)
    ok.record(Path("rec"), Path("e"), Path("r"), "2026-11-19")
    assert calls == [["record", "--records", "rec", "--exam", "e", "--result", "r", "--today", "2026-11-19"]]
    _raises(lambda: _script(1, stderr="no\n")[0].record(Path("rec"), Path("e"), Path("r"), "d"), "no")


def test_grades_must_be_a_regular_file_with_a_grade_for_every_text_question(tmp_path):
    texts = [{"id": "q1"}]
    out = tmp_path / "grades.json"
    assert exams._grades(out, texts) == (None, "the grading session wrote no regular grades file")
    (tmp_path / "elsewhere.json").write_text('{"q1": {"grade": "pass"}}')
    out.symlink_to(tmp_path / "elsewhere.json")
    assert exams._grades(out, texts)[0] is None
    out.unlink()
    for bad in ('not json', '[]', '{"q2": {"grade": "pass"}}', '{"q1": {"grade": "great"}}'):
        out.write_text(bad)
        assert exams._grades(out, texts) == (None, "the grading session's grades are malformed")
    out.write_text('{"q1": {"grade": "partial"}}')
    assert exams._grades(out, texts) == ({"q1": {"grade": "partial"}}, "")
