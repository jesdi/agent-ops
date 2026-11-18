"""Starting, continuing and parking a session: the stage prompt and its queued mail,
login and input parks, code injection, the spec note and the stage signal a launch writes."""
import json
import subprocess
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import eventlog, launch_choice, spec_publish, stage_launch
from dispatcher import intents as intents_mod
from dispatcher.github import Candidate
from dispatcher.state import (NO_SLOT, PlanApprovalRequest, SessionRecord, Stage, has_waiting,
                              load, mark_waiting, operator_request, save, write_session)
from dispatcher.status import Kind
from tests.dispatchfakes import (ADMIT_ALL, LOGIN_TAIL, FakeGitHub, FakeNotifier, FakeSessions,
                                 arm_gate, cfg, deps, gate_signal, make_task, patch_usage,
                                 patch_workspace, replace_capacity, write_tickets)
from tests.statuses import held, human, login, waking
from tests.test_containers import TOKENS
# Bound at import, before tests/conftest.py's _no_podman fake replaces it.
from dispatcher.sessions import _podman as real_podman

ARMED = PlanApprovalRequest(".agent/review.html")

def test_queue_message_drops_blank_text_only(tmp_path):
    c = cfg(tmp_path)
    from dispatcher import messages
    stage_launch._queue_message(c, "portfolio_eval", 42, "", "dispatcher")
    stage_launch._queue_message(c, "portfolio_eval", 42, "   \n ", "dispatcher")
    stage_launch._queue_message(c, "portfolio_eval", 42, "use the staging URL", "jesdi@github")
    assert [m.text for m in messages.all_messages(c.state_dir, "portfolio_eval", 42)] == [
        "use the staging URL"]



def test_park_for_input_appends_parked_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "need a decision"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    parked = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "parked"]
    assert len(parked) == 1
    assert parked[0]["issue"] == 42 and parked[0]["stage"] == "implement"
    assert parked[0]["detail"] == "need a decision"



def test_a_login_hold_whose_ping_fails_stays_held(tmp_path, monkeypatch):
    # The notice follows the commit (ADR 0015), so a failed send can no
    # longer turn the hold into a plain park: the task stays held at its
    # prompt, where the console reaches it.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC)
    sess = FakeSessions(alive=[42], idle={42: 999999.0}, tail=LOGIN_TAIL)
    d = deps(sess=sess, notifier=FakeNotifier(msg_id=0))
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.park_msg_id) == (Kind.HELD_LOGIN, 0)
    assert sess.ended == []
    assert d.notifier.sent == ["needs_relogin"]



def _login_park(tmp_path, msg_id=77, **kw):
    from dispatcher.relogin import LoginPrompt
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, **kw)
    d = deps(sess=FakeSessions(alive=[42], tail=LOGIN_TAIL),
             notifier=FakeNotifier(msg_id=msg_id))
    task = load(c.state_dir, "portfolio_eval", 42)
    held_ = stage_launch._park_for_login(c, d, c.repositories[0], task, "stalled",
                                 LOGIN_TAIL, LoginPrompt(url=""))
    return c, d, held_



def test_park_for_login_holds_the_slotted_task_at_its_prompt(tmp_path):
    c, d, held_ = _login_park(tmp_path)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert held_ and (t.status.kind, t.park_msg_id) == (Kind.HELD_LOGIN, 77)
    assert d.sessions.ended == []
    _, ctx = d.notifier.calls[0]
    assert ctx["login_url"] == "(attach to the session to see the URL)"
    assert ctx["note"].startswith("stalled\n\n")



def test_park_for_login_leaves_a_slotless_task_to_the_generic_park(tmp_path):
    c, d, held_ = _login_park(tmp_path, slot=NO_SLOT, status=waking())
    assert not held_ and d.notifier.sent == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING



def test_park_for_login_leaves_an_undelivered_ping_to_the_generic_park(tmp_path):
    c, d, held_ = _login_park(tmp_path, msg_id=0)
    assert not held_ and d.notifier.sent == ["needs_relogin"]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING



def test_login_park_holds_capacity_against_new_claims(tmp_path, monkeypatch):
    # A login park keeps its container running, so it must NOT free a slot
    # for a new claim — a box-wide auth expiry would otherwise spawn a fresh
    # doomed session on every pass.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, stage=Stage.SPEC, status=login(), park_msg_id=77)
    gh = FakeGitHub([Candidate(99, "X", "u")])
    main.run_pass(c, deps(gh, FakeSessions(alive=[42], tail=LOGIN_TAIL)))
    assert gh.claimed == []



def test_reply_to_login_park_injects_code(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, status=login(), park_msg_id=77)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=77,
                                               text="oauth-code-abc#123")])
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    assert sess.sent_text == [(42, "oauth-code-abc#123")]
    assert sess.resumed == []          # must NOT go through resume
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and t.park_msg_id == 0



def test_injection_refused_when_pane_left_the_login_prompt(tmp_path, monkeypatch):
    # The pane is a HOST tmux session: once claude exits, `has-session` still
    # says alive and send_text would run the operator's code as a shell
    # command outside the sandbox.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, status=login(), park_msg_id=77)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=77, text="code")])
    sess = FakeSessions(alive=[42], tail="agent@box:~$ ")
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert sess.sent_text == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.HELD_LOGIN  # park kept, not cleared
    assert "status" in d.notifier.sent



def test_injection_restores_the_unpark_invariant(tmp_path, monkeypatch):
    # Same invariant _drive_wakes/_retry_stage enforce: a stale blocked signal
    # or waiting marker would re-park (and END) the freshly re-authed session.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT, status=login(), park_msg_id=77)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "x"}))
    mark_waiting(c.state_dir, "portfolio_eval", 42)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=77, text="code")])
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING
    assert not has_waiting(c.state_dir, "portfolio_eval", 42)
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "working"
    assert sess.ended == []             # NOT re-parked over the live session



def test_login_park_clears_the_waiting_marker(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "working"}))
    mark_waiting(c.state_dir, "portfolio_eval", 42)
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL, idle={42: 30.0})
    sess.agent_state = lambda repository, issue: ("done", 5)
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.HELD_LOGIN
    assert sess.sent_text == []         # nothing typed into the login prompt
    assert not has_waiting(c.state_dir, "portfolio_eval", 42)



def test_injection_logs_a_distinct_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, status=login(), park_msg_id=77)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=77, text="code")])
    main.run_pass(c, deps(sess=FakeSessions(alive=[42], tail=LOGIN_TAIL)))
    events = [e["event"] for e in eventlog.read_tail(c.state_dir)]
    assert "login-code-injected" in events
    assert "resumed" not in events



def test_unparseable_timestamp_never_expires(tmp_path, monkeypatch):
    # Fail closed: a corrupt updated_at must not silently park everything.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN, status=held(ARMED),
                   updated_at="not-a-timestamp")
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.HELD_PLAN_APPROVAL and t.updated_at == "not-a-timestamp"
    assert operator_request(t) is not None and t.plan_retries == 0
    assert sess.ended == [] and sess.resumed == [] and d.notifier.sent == []



def test_plan_parked_note_says_local_only_when_publish_fails(
        tmp_path, monkeypatch):
    """_park_for_review must re-run ensure_published and surface its failure
    in the morning ping so the operator knows the link is broken, not silently
    embed a 404."""
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, stage=Stage.PLAN, status=held(ARMED),
                   spec_path="docs/superpowers/specs/x-design.md")
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    monkeypatch.setattr(
        spec_publish, "ensure_published",
        lambda **kw: spec_publish.PublishResult(error="git push failed: auth"))
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    (tmpl, ctx), = [x for x in d.notifier.contexts if x[0] == "plan_parked"]
    assert "⚠️ spec is local only: git push failed: auth" in ctx["note"]
    # No URL anywhere — the operator must not see a link that 404s.
    assert "https://github.com" not in ctx["note"]



def test_spec_error_redacts_tokenized_url_in_note(tmp_path, monkeypatch):
    """A tokenized remote URL in push stderr must not reach the Telegram note."""
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, stage=Stage.PLAN, status=held(ARMED),
                   spec_path="docs/specs/x-design.md")
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    token_error = (
        "git push failed: fatal: unable to access "
        "'https://x-access-token:ghp_SECRET@github.com/jesdi/r.git/': "
        "The requested URL returned error: 403"
    )
    monkeypatch.setattr(
        spec_publish, "ensure_published",
        lambda **kw: spec_publish.PublishResult(error=token_error))
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    (tmpl, ctx), = [x for x in d.notifier.contexts if x[0] == "plan_parked"]
    assert "ghp_SECRET" not in ctx["note"]
    assert "⚠️ spec is local only:" in ctx["note"]



def test_park_for_input_saves_park_note(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "need a decision"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, "portfolio_eval", 42).status.note == "need a decision"



def test_spawn_appends_queued_messages_to_the_stage_prompt(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED)
    from dispatcher import messages
    messages.append(c.state_dir, "portfolio_eval", 42, "pre-brief: use the v2 API", "jesdi@github")
    d = deps()
    task = load(c.state_dir, "portfolio_eval", 42)
    stage_launch._spawn_stage(c, d, c.repositories[0], task,
                      launch_choice._launch_for(c, c.repositories[0], task, Stage.SPEC, lambda m: True, tuple), gh_config=TOKENS)
    prompt = d.sessions.spawned[-1][3]
    assert "## Operator messages" in prompt
    assert "pre-brief: use the v2 API" in prompt
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42)[0].delivered_at != ""



def test_spawn_without_messages_leaves_the_prompt_untouched(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED)
    d = deps()
    task = load(c.state_dir, "portfolio_eval", 42)
    stage_launch._spawn_stage(c, d, c.repositories[0], task,
                      launch_choice._launch_for(c, c.repositories[0], task, Stage.SPEC, lambda m: True, tuple), gh_config=TOKENS)
    assert "## Operator messages" not in d.sessions.spawned[-1][3]



def test_resume_delivers_every_queued_message_oldest_first(tmp_path):
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, status=waking(), slot=NO_SLOT)
    from dispatcher import messages
    messages.append(c.state_dir, "portfolio_eval", 42, "first", "jesdi@github")
    messages.append(c.state_dir, "portfolio_eval", 42, "second", "jesdi@github")
    d = deps()
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    text = d.sessions.resumed[-1][1]
    assert text.index("first") < text.index("second")
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []



def test_resume_with_an_empty_queue_still_says_continue(tmp_path):
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, status=waking(), slot=NO_SLOT)
    d = deps()
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    assert d.sessions.resumed[-1][1].split("\n\n")[0] == "Continue."



def test_retry_plan_delivers_queued_messages_too(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN)
    (wt / ".agent" / "tickets").mkdir()
    (wt / ".agent" / "tickets" / "01-bad.md").write_text("# tiny\n")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-review", "artifact": ".agent/review.html"}))
    from dispatcher import messages
    messages.append(c.state_dir, "portfolio_eval", 42, "keep the scope small", "jesdi@github")
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    assert "keep the scope small" in d.sessions.resumed[-1][1]
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []



def test_delivery_does_not_stamp_messages_queued_after_the_drain(tmp_path):
    """A message that lands between the drain and the stamp must survive."""
    c = cfg(tmp_path)
    make_task(c, issue=42, status=waking(), slot=NO_SLOT)
    from dispatcher import messages
    messages.append(c.state_dir, "portfolio_eval", 42, "delivered now", "jesdi@github")
    d = deps()
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    messages.append(c.state_dir, "portfolio_eval", 42, "arrived later", "jesdi@github")
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [
        "arrived later"]



def test_park_for_input_frees_the_slot(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=1)
    stage_launch._park_for_input(c, deps(), c.repositories[0], load(c.state_dir, "portfolio_eval", 42),
                         human("decision", "which redirect URL?"))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN and t.slot == NO_SLOT



@pytest.mark.parametrize("podman", ["hangs", "fails"])
def test_park_for_input_parks_when_podman_rm_hangs_or_fails(tmp_path, monkeypatch, podman):
    """The question is already on Telegram when the session ends, so a podman
    rm that times out or exits non-zero must not turn the park into a failure
    (agent-ops#43, #49, #83). A container left behind beats a lost park, and
    is surfaced, since the park frees the slot."""
    from dispatcher import sessions as sessions_mod

    def run(args, **kw):
        if podman == "hangs":
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        return subprocess.CompletedProcess(args, 125, "", "Error: lock timeout")
    monkeypatch.setattr(sessions_mod, "_podman", real_podman)  # the real one, over subprocess
    monkeypatch.setattr(sessions_mod.subprocess, "run", run)
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=1)
    stage_launch._park_for_input(c, deps(sess=sessions_mod.Sessions(state_dir=c.state_dir)),
                         c.repositories[0], load(c.state_dir, "portfolio_eval", 42),
                         human("decision", "which redirect URL?"))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN and t.slot == NO_SLOT and t.park_msg_id == 77
    assert "teardown-failed" in [e["event"] for e in eventlog.read_tail(c.state_dir)]



def test_console_reply_to_a_login_park_types_the_code(tmp_path, monkeypatch):
    """PARK_LOGIN is the one park whose "reply" is an OAuth authorization
    code. Queueing it would promise a delivery that cannot work (the resume
    ends the pane the code was for) and would persist a single-use credential
    at rest, rendered in the console thread."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.SPEC, status=login(), park_msg_id=77)
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                             {"text": "oauth-code-abc#123"}, "jesdi@github", 1)
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    from dispatcher import messages
    assert sess.sent_text == [(42, "oauth-code-abc#123")]
    assert sess.resumed == []                   # never through the wake path
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42) == []  # not persisted at rest
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING



def test_spawn_stage_clears_review_gate_e2e_but_retains_ci(tmp_path):
    c = cfg(tmp_path)
    make_task(c, stage=Stage.QUEUED, review_rounds=1, gate_rounds=2,
              e2e_rounds=1, ci_rounds=3)
    d = deps()
    task = load(c.state_dir, "portfolio_eval", 42)
    stage_launch._spawn_stage(c, d, c.repositories[0], task,
                      launch_choice._launch_for(c, c.repositories[0], task, Stage.SPEC, lambda m: True, tuple), gh_config=TOKENS)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds) == (0, 0, 0)
    assert t.ci_rounds == 3  # ci belongs to the PR, not the stage



def test_spawn_stage_clears_operator_request_but_preserves_spec_path(tmp_path):
    """Slice 14: advancing stage via _spawn_stage must clear operator_request
    while preserving spec_path."""
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.PLAN, status=held(PlanApprovalRequest(".agent/review.html")),
                   spec_path="docs/specs/design.md")
    d = deps()
    task = load(c.state_dir, "portfolio_eval", 42)
    stage_launch._spawn_stage(c, d, c.repositories[0], task,
                      launch_choice._launch_for(c, c.repositories[0], task, Stage.IMPLEMENT,
                                       lambda m: True, tuple), gh_config=TOKENS)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(t) is None, (
        f"stage advance must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved on stage advance"



# --- the dispatcher never writes through a session's symlink -----------------

def _victim(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    (out / "victim.json").write_text("untouched")
    return out



def _working_signal_is_a_regular_file(wt):
    f = wt / ".agent" / "stage.json"
    assert not f.is_symlink() and json.loads(f.read_text())["status"] == "working"



def test_symlink_at_the_stage_signal_is_replaced_on_spawn_and_on_resume(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    out = _victim(tmp_path)
    # Spawn: a gate task whose session died gets a fresh plan session.
    wt = make_task(c, issue=42, stage=Stage.PLAN, gated=True)
    (wt / ".agent" / "stage.json").symlink_to(out / "victim.json")
    # Resume: a parked implement task the operator woke.
    wt2 = make_task(c, issue=43, stage=Stage.IMPLEMENT, status=waking(), slot=NO_SLOT)
    (wt2 / ".agent" / "stage.json").symlink_to(out / "victim.json")
    (wt2 / ".agent" / "models.log").symlink_to(out / "victim.json")
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert (out / "victim.json").read_text() == "untouched"
    assert [s[:2] for s in sess.spawned] == [(42, "plan")]
    _working_signal_is_a_regular_file(wt)
    # the planted log link fails that task's resume; nothing left the worktree
    assert load(c.state_dir, "portfolio_eval", 43).status.kind is Kind.FAILED
    (wt2 / ".agent" / "models.log").unlink()
    save(c.state_dir, dc_replace(load(c.state_dir, "portfolio_eval", 43),
                                 stage=Stage.IMPLEMENT, status=waking()))
    (wt2 / ".agent" / "stage.json").unlink()
    (wt2 / ".agent" / "stage.json").symlink_to(out / "victim.json")
    main.run_pass(c, deps(sess=sess))
    assert (out / "victim.json").read_text() == "untouched"
    assert [r[0] for r in sess.resumed] == [43]
    _working_signal_is_a_regular_file(wt2)



def test_agent_directory_that_is_a_symlink_fails_the_launch_and_not_the_pass(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    out = _victim(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN, gated=True)
    (wt / ".agent").rmdir()
    (wt / ".agent").symlink_to(out, target_is_directory=True)
    wt2 = make_task(c, issue=43, stage=Stage.IMPLEMENT, slot=1)
    (wt2 / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "done"}))
    gh, sess = FakeGitHub(), FakeSessions(alive={43})
    main.run_pass(c, deps(gh, sess))             # must not raise
    assert sorted(p.name for p in out.iterdir()) == ["victim.json"]
    assert (out / "victim.json").read_text() == "untouched"
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.FAILED and [s[0] for s in sess.spawned] == [43]
    assert "not a real directory" in gh.created_issues[0][2]
    assert load(c.state_dir, "portfolio_eval", 43).stage is Stage.REVIEW



def test_launch_and_resume_write_the_exclude_lines_in_the_configured_clone_only(
        tmp_path, monkeypatch):
    """The lines heal on every launch and resume, at the clone the repository's
    configuration names. The worktree's own `.git` pointer, which a session
    can rewrite, decides nothing."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    info = Path(c.repositories[0].clone_path) / ".git" / "info"
    info.mkdir(parents=True)
    outside = tmp_path / "outside-git"
    (outside / "info").mkdir(parents=True)
    wt = make_task(c, issue=42, stage=Stage.PLAN, gated=True)        # spawned
    wt2 = make_task(c, issue=43, stage=Stage.IMPLEMENT, status=waking(), slot=NO_SLOT)
    for w in (wt, wt2):
        (w / ".git").write_text(f"gitdir: {outside}\n")
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [s[0] for s in sess.spawned] == [42] and [r[0] for r in sess.resumed] == [43]
    assert list((outside / "info").iterdir()) == []
    assert (info / "exclude").read_text().splitlines() == [
        ".agent/", ".claude/settings.local.json"]



def test_unusable_clone_git_dir_does_not_stop_a_resume(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    outside = tmp_path / "outside-git"
    (outside / "info").mkdir(parents=True)
    clone = Path(c.repositories[0].clone_path)
    clone.mkdir()
    (clone / ".git").symlink_to(outside, target_is_directory=True)
    make_task(c, issue=43, stage=Stage.IMPLEMENT, status=waking(), slot=NO_SLOT)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [r[0] for r in sess.resumed] == [43]
    assert load(c.state_dir, "portfolio_eval", 43).status.kind is Kind.RUNNING
    assert list((outside / "info").iterdir()) == []
