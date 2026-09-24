"""Acceptance tests for ticket 03: messages and slot markers keyed by
(target, issue) instead of a bare issue number.

Spec: specs/multi-project-box/spec.md requirement 10 and its four scenarios
("a reply reaches only its own project", "a slot-starved task marks only its
own card", "legacy files migrate while one target is configured", "legacy
message files are not guessed at with several targets").
Design: specs/multi-project-box/design.md, "Forced by red-team" and "Legacy
key migration".

These tests assume interface shapes the ticket leaves to the implementer,
per design.md's Seams section ("dispatcher.messages ... now taking target"
and "web.sources.Sources.messages(target, issue) / undelivered_counts() /
wake_blocked_issues()"). Documented here so the implementer can hold to them
or deliberately diverge:

- dispatcher.messages.append/undelivered/mark_delivered/all_messages gain a
  `target: str` positional parameter placed right after `state_dir`, matching
  every other (state_dir, target, issue, ...) seam in this codebase (e.g.
  execution_overrides.load, task_artifacts.read). New on-disk path:
  messages/{target}-{issue}.jsonl.
- web.sources.Sources.messages(target, issue) — was messages(issue).
- web.sources.Sources.undelivered_counts() returns dict[tuple[str, int], int]
  keyed by (target, issue) — was dict[int, int]. This is the fix for the
  cross-target gap web/app.py's own comment names at the `mail.get(t.issue,
  0)` call sites.
- The wake-blocked marker file becomes wake-blocked-{target}-{issue} (was
  wake-blocked-{issue}), and Sources.wake_blocked_issues() parses the target
  out of the filename directly instead of cross-joining every bare-issue
  marker against every configured target.
- Legacy migration (single target: rename messages/{issue}.jsonl and
  wake-blocked-{issue} to the target-keyed name; 2+ targets: drop the legacy
  wake-blocked marker, leave the legacy message file in place with a stderr
  warning naming it) runs once at the top of dispatcher.main.run_pass.
"""
import json
from dataclasses import replace as dc_replace
from pathlib import Path

import dispatcher.main as main
from dispatcher import messages
from dispatcher.state import NO_SLOT, PARK_WAKE, Stage, TaskState, save
from tests import webfakes
from tests.test_main import ADMIT_ALL, cfg as single_target_cfg, deps as main_deps, patch_usage
from web.sources import Sources


def two_target_cfg(tmp_path):
    """portfolio_eval (existing single-target fixture) plus a second
    'factorial' target sharing the same shape, listed in that order."""
    c = single_target_cfg(tmp_path)
    pe = c.targets[0]
    factorial = dc_replace(
        pe, name="factorial", repo="jesdi/factorial",
        clone_path=str(tmp_path / "factorial_repo"),
        worktrees_path=str(tmp_path / "factorial_repo.worktrees"))
    return dc_replace(c, targets=[pe, factorial])


def make_task_for(c, target_name, issue, stage=Stage.IMPLEMENT, slot=0,
                  updated_at="2026-07-21T00:00:00+00:00", track="standard",
                  **kw):
    target = next(t for t in c.targets if t.name == target_name)
    wt = Path(target.worktrees_path) / f"task-{issue}"
    (wt / ".agent").mkdir(parents=True, exist_ok=True)
    ts = TaskState(issue=issue, target=target_name, stage=stage, slot=slot,
                   worktree=str(wt), branch=f"agent/task-{issue}", title="t",
                   updated_at=updated_at, track=track, **kw)
    save(c.state_dir, ts)
    return wt


def _legacy_message_file(state_dir, issue, text):
    p = Path(state_dir) / "messages" / f"{issue}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "id": "legacy-1", "text": text, "actor": "operator",
        "created_at": "2026-01-01T00:00:00+00:00", "delivered_at": None,
    }) + "\n")
    return p


# -- checkbox 1: delivery isolation via run_pass -----------------------------

def test_reply_reaches_only_its_own_project(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = two_target_cfg(tmp_path)
    make_task_for(c, "portfolio_eval", issue=7, park=PARK_WAKE)
    make_task_for(c, "factorial", issue=7, park=PARK_WAKE)

    messages.append(c.state_dir, "factorial", 7, "use the v2 endpoint",
                    "operator")

    d = main_deps()
    main.run_pass(c, d)

    assert len(d.sessions.resumed) == 2
    prompts = {target: message for (target, issue), (_, message, *_)
               in zip(d.sessions.resume_calls, d.sessions.resumed)
               if issue == 7}
    assert "use the v2 endpoint" not in prompts["portfolio_eval"]
    assert "use the v2 endpoint" in prompts["factorial"]
    assert [bool(m.delivered_at) for m in
            messages.all_messages(c.state_dir, "factorial", 7)] == [True]
    assert messages.all_messages(c.state_dir, "portfolio_eval", 7) == []


# -- checkbox 2: console thread + unread count via web.sources.Sources ------

def test_sources_messages_and_mail_count_are_target_scoped(tmp_path):
    cfg_ = webfakes.make_config(
        tmp_path / "state",
        targets=[webfakes.make_target("portfolio_eval", "jesdi/portfolio_eval"),
                 webfakes.make_target("factorial", "jesdi/factorial")])
    # Write the queue file directly at the NEW target-keyed path, so this
    # test's failure is attributable to Sources' own (still bare-issue)
    # signature rather than to dispatcher.messages'.
    p = Path(cfg_.state_dir) / "messages" / "factorial-7.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "id": "m1", "text": "use the v2 endpoint", "actor": "operator",
        "created_at": "2026-01-01T00:00:00+00:00", "delivered_at": None,
    }) + "\n")

    src = Sources(cfg_, sessions=None, github=None)

    fa_msgs = src.messages("factorial", 7)
    assert [m.text for m in fa_msgs] == ["use the v2 endpoint"]
    assert src.messages("portfolio_eval", 7) == []
    assert src.undelivered_counts() == {("factorial", 7): 1}


# -- checkbox 3: slot marker is scoped to its own target ---------------------

def test_wake_blocked_marker_is_scoped_to_its_own_target(tmp_path):
    c = two_target_cfg(tmp_path)
    c = dc_replace(c, capacity=1)
    # The box is full (two active tasks, capacity 1), so factorial#12 stays
    # woken and is marked — the marker must not leak to portfolio_eval#12.
    make_task_for(c, "factorial", issue=99, slot=0, park="")
    make_task_for(c, "factorial", issue=12, slot=NO_SLOT, park=PARK_WAKE)
    make_task_for(c, "portfolio_eval", issue=12, slot=0, park="")

    main._resume_woken(c, main_deps(), admit=ADMIT_ALL)

    src = Sources(
        webfakes.make_config(
            c.state_dir,
            targets=[webfakes.make_target("portfolio_eval", "jesdi/portfolio_eval"),
                     webfakes.make_target("factorial", "jesdi/factorial")]),
        sessions=None, github=None)
    blocked = src.wake_blocked_issues()

    assert ("factorial", 12) in blocked
    assert ("portfolio_eval", 12) not in blocked


# -- checkbox 4: legacy migration with one target ----------------------------

def test_legacy_message_file_migrates_with_a_single_target(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = single_target_cfg(tmp_path)
    make_task_for(c, "portfolio_eval", issue=412, park=PARK_WAKE)
    _legacy_message_file(c.state_dir, 412, "drop the caching layer")

    d = main_deps()
    main.run_pass(c, d)

    new_path = Path(c.state_dir) / "messages" / "portfolio_eval-412.jsonl"
    old_path = Path(c.state_dir) / "messages" / "412.jsonl"
    assert new_path.exists(), "legacy file was not migrated to the target-keyed name"
    assert not old_path.exists(), "legacy bare-issue file was left behind"
    assert any(issue == 412 and "drop the caching layer" in message
              for issue, message, *_ in d.sessions.resumed)


# -- checkbox 5: legacy files are not guessed at with several targets -------

def test_legacy_message_file_is_not_migrated_with_two_targets(tmp_path, monkeypatch, capsys):
    patch_usage(monkeypatch)
    c = two_target_cfg(tmp_path)
    make_task_for(c, "portfolio_eval", issue=3, park=PARK_WAKE)
    make_task_for(c, "factorial", issue=3, park=PARK_WAKE)
    legacy = _legacy_message_file(c.state_dir, 3, "ambiguous reply")

    d = main_deps()
    main.run_pass(c, d)

    assert len(d.sessions.resumed) == 2
    assert not any("ambiguous reply" in message
                   for _, message, *_ in d.sessions.resumed)

    assert legacy.exists(), "an unattributable legacy file must be left in place"
    assert not (Path(c.state_dir) / "messages" / "portfolio_eval-3.jsonl").exists()
    assert not (Path(c.state_dir) / "messages" / "factorial-3.jsonl").exists()
    err = capsys.readouterr().err
    assert "3.jsonl" in err, f"expected a warning naming the legacy file, got: {err!r}"
