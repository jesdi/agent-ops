"""Locked, black-box acceptance tests for ticket 10: a gated second model for
Claude review sessions. See .agent/tickets/10-review-second-model.md and
docs/specs/2026-09-24-codex-runtime-design.md ("Second model in Claude review
sessions", "Launcher", "Testing").

Interface these tests define (the Seam):
- `dispatcher.models.second_model(policy, stage, entry, admitted) -> Entry | None`
  — pure; `stage` is the stage name string, `admitted` a model-id predicate.
- `containers.session_cmd(..., second: Entry | None = None)`,
  `Sessions.spawn_stage(..., second: Entry | None = None)` and
  `Sessions.resume(..., second: Entry | None = None)` carry the grant; the
  dispatcher passes it as the keyword `second`."""
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import containers, execution_overrides, herdr, sessions
from dispatcher.models import Entry, parse_policy
from dispatcher.state import PARK_WAKE, Stage, TaskState, load, save
from tests.test_containers import make_worktree
from tests.test_main import FakeSessions, cfg, deps, make_task
from tests.usagefakes import session_usage

SECOND = Entry("openai", "gpt-5-codex")

POLICY = parse_policy({
    "triage": ["claude-opus-5"],
    "untracked": "standard",
    "review_second": "openai/gpt-5-codex",
    "tracks": {
        "standard": {"when": "Everything.",
                     "spec": ["claude-opus-5"], "plan": ["claude-opus-5"],
                     "implement": ["claude-opus-5"], "review": ["claude-opus-5"]},
        "codexreview": {"when": "Codex reviews.",
                        "spec": ["claude-opus-5"], "plan": ["claude-opus-5"],
                        "implement": ["claude-opus-5"],
                        "review": ["openai/gpt-5-codex"]},
    },
})
UNSET = replace(POLICY, review_second="")
CLAUDE = Entry("anthropic", "claude-opus-5")
CODEX = Entry("openai", "gpt-5-codex")


def admit_all(model_id: str) -> bool:
    return True


def deny_openai(model_id: str) -> bool:
    return not model_id.startswith("openai/")


def _second_model():
    """Resolved at call time so a missing name fails the test, not collection."""
    from dispatcher.models import second_model
    return second_model


# --- 1. review + anthropic entry + key set + admitted -> the entry ---------

def test_second_model_granted_on_admitted_anthropic_review():
    assert _second_model()(POLICY, "review", CLAUDE, admit_all) == SECOND


# --- 2. gate denies review_second -> None ----------------------------------

def test_second_model_none_when_gate_denies_it():
    assert _second_model()(POLICY, "review", CLAUDE, deny_openai) is None


# --- 3. review_second unset -> None ----------------------------------------

def test_second_model_none_when_unset():
    assert _second_model()(UNSET, "review", CLAUDE, admit_all) is None


# --- 4. any non-review stage, including address-review -> None -------------

@pytest.mark.parametrize("stage", ["spec", "plan", "implement", "address-review"])
def test_second_model_none_for_non_review_stage(stage):
    assert _second_model()(POLICY, stage, CLAUDE, admit_all) is None


# --- 5. a Codex (openai/...) stage entry -> None ---------------------------

def test_second_model_none_for_codex_entry():
    assert _second_model()(POLICY, "review", CODEX, admit_all) is None


# --- dispatcher rig ----------------------------------------------------------

class GrantSessions(FakeSessions):
    """FakeSessions that also records the `second` grant per call."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.spawn_seconds, self.resume_seconds = [], []

    def spawn_stage(self, target, issue, worktree, prompt, stage_name, model,
                    effort="", second=None):
        super().spawn_stage(target, issue, worktree, prompt, stage_name, model, effort)
        self.spawn_seconds.append((stage_name, second))

    def resume(self, target, issue, worktree, message, model, effort="",
               second=None):
        super().resume(target, issue, worktree, message, model, effort)
        self.resume_seconds.append(second)


def _cfg(tmp_path):
    return replace(cfg(tmp_path), models=POLICY)


def _verdict(openai_ok: bool, anthropic_ok: bool = True):
    def admit(model_id):
        ok = openai_ok if model_id.startswith("openai/") else anthropic_ok
        return main.Verdict(admitted=ok, provider=model_id.split("/")[0],
                            binding=None, reason="ok" if ok else "unavailable")
    return admit


def _usages(monkeypatch, openai_ok: bool):
    usages = {"anthropic": session_usage(0.2)}
    if openai_ok:  # a missing provider fails closed at the gate
        usages["openai"] = session_usage(0.2, provider="openai")
    monkeypatch.setattr(main, "fetch_all", lambda cfg, **k: usages)


def _implement_done(c, issue=2):
    """An implement task whose last ticket is done: the next pass spawns review."""
    wt = Path(c.targets[0].worktrees_path) / f"task-{issue}"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "done", "note": ""}))
    save(c.state_dir, TaskState(issue=issue, target="portfolio_eval",
                                stage=Stage.IMPLEMENT, slot=1,
                                ticket_cursor=1, ticket_count=1,
                                worktree=str(wt), branch=f"agent/task-{issue}",
                                title="B", track="standard",
                                updated_at="2026-07-14T00:00:00+00:00"))
    return wt


def _review_spawns(sess):
    return [s for s in sess.spawn_seconds if s[0] == "review"]


# --- 6. operator bypass_usage does not grant the second model ---------------

@pytest.mark.parametrize("openai_ok, expected", [(False, None), (True, SECOND)])
def test_bypassed_review_spawn_gets_second_only_if_gate_admits_it(
        tmp_path, monkeypatch, openai_ok, expected):
    c = _cfg(tmp_path)
    _usages(monkeypatch, openai_ok)
    _implement_done(c)
    execution_overrides.save(c.state_dir, "portfolio_eval", 2,
                             execution_overrides.ExecutionOverride(bypass_usage=True))
    sess = GrantSessions(alive={2})
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, "portfolio_eval", 2).stage is Stage.REVIEW
    assert _review_spawns(sess) == [("review", expected)]


def test_bypassed_review_resume_gets_second_only_if_gate_admits_it(tmp_path):
    c = _cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, park=PARK_WAKE,
              resume_bypass_usage=True)
    sess = GrantSessions()
    # Anthropic denied too: only the bypass lets the review resume at all.
    main._resume_woken(c, deps(sess=sess),
                       admit=_verdict(openai_ok=False, anthropic_ok=False))
    assert sess.resume_seconds == [None]
    make_task(c, issue=42, stage=Stage.REVIEW, park=PARK_WAKE,
              resume_bypass_usage=True)
    main._resume_woken(c, deps(sess=sess),
                       admit=_verdict(openai_ok=True, anthropic_ok=False))
    assert sess.resume_seconds == [None, SECOND]


# --- 7. session_cmd with a grant mounts codex binary + codex-home ----------

def _fake_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    real_codex = home / "opt" / "codex-1.2.3"
    real_codex.parent.mkdir(parents=True)
    real_codex.write_text("")
    (home / ".local" / "bin" / "codex").symlink_to(real_codex)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    return f"{os.path.realpath(real_codex)}:/usr/local/bin/codex:ro"


def _assert_granted(cmd, tmp_path, codex_mount):
    assert f"-v {codex_mount}" in cmd
    assert f"-v {tmp_path / 'state'}/codex-home:/root/.codex" in cmd
    assert "-e CODEX_HOME=/root/.codex" in cmd
    # still a Claude session
    assert f"-v {tmp_path / 'state'}/claude-home:" in cmd
    assert "--model claude-opus-5" in cmd


def test_session_cmd_with_grant_mounts_codex_and_codex_home(tmp_path, monkeypatch):
    codex_mount = _fake_codex_home(tmp_path, monkeypatch)
    wt, _ = make_worktree(tmp_path)
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "claude-opus-5", "P",
                                 second=SECOND)
    _assert_granted(cmd, tmp_path, codex_mount)


class _Tab:
    alive = True

    def __init__(self, sink):
        self.sink = sink

    def run(self, cmd):
        self.sink.append(cmd)
        return True


def test_sessions_spawn_and_resume_carry_the_grant_to_the_command(tmp_path, monkeypatch):
    codex_mount = _fake_codex_home(tmp_path, monkeypatch)
    wt, _ = make_worktree(tmp_path)
    cmds = []
    monkeypatch.setattr(herdr.Tab, "ensure", lambda *a, **k: _Tab(cmds))
    s = sessions.Sessions()
    s.spawn_stage("acme", 42, wt, "P", "review", "anthropic/claude-opus-5",
                  second=SECOND)
    s.resume("acme", 42, wt, "go", "anthropic/claude-opus-5", second=SECOND)
    s.resume("acme", 42, wt, "go", "anthropic/claude-opus-5")
    assert len(cmds) == 3
    _assert_granted(cmds[0], tmp_path, codex_mount)
    _assert_granted(cmds[1], tmp_path, codex_mount)
    assert "codex" not in cmds[2].lower()


# --- 8. without a grant: byte-identical to a plain Claude session today ----

def test_session_cmd_without_grant_is_byte_identical(tmp_path, monkeypatch):
    _fake_codex_home(tmp_path, monkeypatch)
    wt, _ = make_worktree(tmp_path)
    args = ("task-42", wt, "2g", "2", "claude-opus-5", "P")
    today = containers.session_cmd(*args, effort="high")
    assert containers.session_cmd(*args, effort="high", second=None) == today
    assert "codex" not in today.lower()


# --- 9. spawn and resume each re-decide the grant at that moment -----------

@pytest.mark.parametrize("openai_ok, expected", [(True, SECOND), (False, None)])
def test_review_spawn_decides_grant_at_spawn(tmp_path, monkeypatch, openai_ok, expected):
    c = _cfg(tmp_path)
    _usages(monkeypatch, openai_ok)
    _implement_done(c)
    sess = GrantSessions(alive={2})
    main.run_pass(c, deps(sess=sess))
    assert _review_spawns(sess) == [("review", expected)]


def test_review_resume_re_asks_the_gate_each_time(tmp_path, monkeypatch):
    c = _cfg(tmp_path)
    # Spawned with the grant...
    _usages(monkeypatch, openai_ok=True)
    _implement_done(c)
    sess = GrantSessions(alive={2})
    main.run_pass(c, deps(sess=sess))
    assert _review_spawns(sess) == [("review", SECOND)]
    # ...resumed after OpenAI ran out: no stored grant carries over.
    task = load(c.state_dir, "portfolio_eval", 2)
    save(c.state_dir, replace(task, park=PARK_WAKE))
    main._resume_woken(c, deps(sess=sess), admit=_verdict(openai_ok=False))
    # ...and resumed again once it is back: granted afresh.
    save(c.state_dir, replace(load(c.state_dir, "portfolio_eval", 2), park=PARK_WAKE))
    main._resume_woken(c, deps(sess=sess), admit=_verdict(openai_ok=True))
    assert sess.resume_seconds == [None, SECOND]
