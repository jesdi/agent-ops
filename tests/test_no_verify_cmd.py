"""Acceptance tests for ticket 07: no pre-PR e2e without verify_cmd.

Black-box, through the prompt actually handed to a spawned session
(`FakeSessions.spawned[-1][3]`) and through `run_pass`'s CI-loop behavior.

Exact phrases the implementation must produce (spec scenario "empty
verify_cmd never reports e2e as passed" and design's "no pre-PR e2e"
fragment). Pick these, not looser ones, so a regression that silently drops
the sentence — or leaves `$verify_cmd`/an empty command in the prompt — is
caught:
"""
from dataclasses import replace as dc_replace

import dispatcher.main as main
from dispatcher.pr_poll import CIStatus
from dispatcher.state import PARK_HUMAN, LoopCaps, Stage, load
from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, cfg,
                             deps, make_task, patch_usage, payload,
                             pr_open_task)

# The scenario's own wording: "the rendered prompt states that no off-box
# e2e run exists for this target". The implementer's "no pre-PR e2e"
# fragment must contain this phrase verbatim.
NO_E2E_PHRASE = "no off-box e2e run exists for this target"

# The PR-body "Verification" line must ask for gate verification and must
# NOT promise an e2e run URL when there is none to promise.
GATE_VERIFICATION_PHRASE = "gate"
E2E_URL_PHRASE = "e2e run URL"


def cfg_no_verify(tmp_path):
    c = cfg(tmp_path)
    return dc_replace(c, targets=[dc_replace(c.targets[0], verify_cmd="")])


def test_review_prompt_without_verify_cmd_states_no_pre_pr_e2e(tmp_path):
    c = cfg_no_verify(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, track="standard")
    d = deps(sess=FakeSessions())
    task = load(c.state_dir, "portfolio_eval", 42)
    main._spawn_stage(c, d, c.targets[0], task,
                      main._launch_for(c, c.targets[0], task, Stage.REVIEW,
                                       lambda m: True))
    prompt = d.sessions.spawned[-1][3]

    assert NO_E2E_PHRASE in prompt
    assert "awaiting-ci" not in prompt
    # No unfilled placeholder and no empty-command artifact from a blank
    # $verify_cmd substitution.
    assert "$verify_cmd" not in prompt
    assert "Run ``" not in prompt


def test_review_prompt_without_verify_cmd_pr_body_asks_gate_not_e2e_url(tmp_path):
    c = cfg_no_verify(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, track="standard")
    d = deps(sess=FakeSessions())
    task = load(c.state_dir, "portfolio_eval", 42)
    main._spawn_stage(c, d, c.targets[0], task,
                      main._launch_for(c, c.targets[0], task, Stage.REVIEW,
                                       lambda m: True))
    prompt = d.sessions.spawned[-1][3]

    assert E2E_URL_PHRASE not in prompt
    assert GATE_VERIFICATION_PHRASE in prompt


def test_address_review_prompt_without_verify_cmd_has_no_e2e_step(tmp_path):
    c = cfg_no_verify(tmp_path)
    make_task(c, issue=42, stage=Stage.ADDRESS_REVIEW, track="standard",
             pr_number=7)
    d = deps(sess=FakeSessions())
    task = load(c.state_dir, "portfolio_eval", 42)
    main._spawn_stage(c, d, c.targets[0], task,
                      main._launch_for(c, c.targets[0], task,
                                       Stage.ADDRESS_REVIEW, lambda m: True))
    prompt = d.sessions.spawned[-1][3]

    assert "awaiting-ci" not in prompt
    assert "$verify_cmd" not in prompt
    assert "Run ``" not in prompt


def test_review_and_address_review_prompts_with_verify_cmd_keep_e2e_step(tmp_path):
    c = cfg(tmp_path)  # verify_cmd="make e2e-slot SLOT={slot}"
    d = deps(sess=FakeSessions())

    make_task(c, issue=42, stage=Stage.REVIEW, track="standard", slot=0)
    task = load(c.state_dir, "portfolio_eval", 42)
    main._spawn_stage(c, d, c.targets[0], task,
                      main._launch_for(c, c.targets[0], task, Stage.REVIEW,
                                       lambda m: True))
    review_prompt = d.sessions.spawned[-1][3]
    assert "awaiting-ci" in review_prompt
    assert "make e2e-slot SLOT=0" in review_prompt
    assert NO_E2E_PHRASE not in review_prompt

    make_task(c, issue=43, stage=Stage.ADDRESS_REVIEW, track="standard",
             pr_number=7, slot=1)
    task = load(c.state_dir, "portfolio_eval", 43)
    main._spawn_stage(c, d, c.targets[0], task,
                      main._launch_for(c, c.targets[0], task,
                                       Stage.ADDRESS_REVIEW, lambda m: True))
    address_review_prompt = d.sessions.spawned[-1][3]
    assert "awaiting-ci" in address_review_prompt
    assert "make e2e-slot SLOT=1" in address_review_prompt
    assert NO_E2E_PHRASE not in address_review_prompt


def test_red_e2e_check_without_verify_cmd_spawns_address_review_and_spends_ci_round(
        tmp_path, monkeypatch):
    """Guard: PR checks are the only e2e signal once verify_cmd is empty, and
    the existing CI loop still drives off them. May already pass."""
    patch_usage(monkeypatch)
    c = cfg_no_verify(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))

    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.ci_rounds, t.check_cursor) == (
        Stage.ADDRESS_REVIEW, 1, "2026-09-07T10:00:00Z")
    assert "pr_attention" in notif.sent
    assert "check-failed" in sess.spawned[-1][3]


def test_red_e2e_check_without_verify_cmd_parks_past_the_ci_cap(tmp_path, monkeypatch):
    """Guard: past loop_caps.ci rounds the task parks instead of spawning
    again. May already pass."""
    patch_usage(monkeypatch)
    c = dc_replace(cfg_no_verify(tmp_path), loop_caps=LoopCaps(ci=1))
    pr_open_task(c, ci_rounds=1)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))

    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.park, t.feedback_pending) == (
        Stage.PR_OPEN, PARK_HUMAN, False)
    assert "parked_question" in notif.sent and not sess.spawned
