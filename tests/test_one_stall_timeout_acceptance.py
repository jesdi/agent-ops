"""Acceptance: one stall timeout and a 5 minute pass (herdr-liveness-parking, ticket 02).

Black-box through load_config and run_pass. Time is controlled through the
fake session's idle seconds; nothing sleeps."""
import json
import re
from dataclasses import replace as dc_replace
from pathlib import Path

import dispatcher.main as main
from dispatcher.config import load_config
from dispatcher.state import PARK_HUMAN, Stage, load, mark_waiting
from tests.test_herdr_liveness_poke_acceptance import (N, POKE, T, HerdrSessions,
                                                       _setup, _signal)
from tests.test_main import cfg, deps, make_task, patch_usage

ROOT = Path(__file__).resolve().parent.parent
MIN = 60
CODEX = {"review": "openai/gpt-sol"}
STALL_NOTE = "(no session output for 10m — likely login/trust prompt or hang)"

BASE = """\
state_dir: {state}
stall_after_seconds: 600
{extra}
projects:
  - name: portfolio-eval
    rank_cmd: rank
    board: {{owner: jesdi, number: 1, status_field_id: F, status_ready_option_id: R,
            status_in_progress_option_id: I}}
    repositories:
      - name: portfolio_eval
        repo: jesdi/portfolio_eval
        github_id: 1
        clone_path: /tmp/c
        worktrees_path: /tmp/w
        setup_cmd: setup
        verify_cmd: "make e2e-slot SLOT={{slot}}"
        gate_cmd: "make gate-slot SLOT={{slot}}"
"""


def _load(tmp_path, extra=""):
    p = tmp_path / "targets.yaml"
    p.write_text(BASE.format(state=tmp_path / "state", extra=extra))
    return load_config(p)


def test_codex_timeout_key_is_ignored_with_one_warning(tmp_path, capsys):
    c = _load(tmp_path, "codex_stall_after_seconds: 1800")
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "codex_stall_after_seconds" in ln]
    assert len(lines) == 1 and "[warn]" in lines[0]
    assert c.stall_after_seconds == 600
    assert not hasattr(c, "codex_stall_after_seconds")


def test_hung_codex_session_parks_after_the_shared_timeout(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), stall_after_seconds=600)
    wt = make_task(c, issue=N, stage=Stage.REVIEW, picks=CODEX)
    _signal(wt, stage="review")
    sess = HerdrSessions(alive=[N])
    sess.stop("idle", idle=11 * MIN)
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, T, N)
    assert t.park == PARK_HUMAN and t.park_note == STALL_NOTE
    assert sess.ended == [N]


def test_stopped_codex_session_with_marker_is_poked(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, picks=CODEX)
    sess.stop("done")
    main.run_pass(c, d)
    assert sess.sent_text == [(N, POKE)]
    assert load(c.state_dir, T, N).park == ""


def test_pass_interval_defaults_to_5_and_status_reports_it(tmp_path, monkeypatch):
    c = _load(tmp_path)
    assert c.pass_interval_minutes == 5
    patch_usage(monkeypatch)
    main.run_pass(c, deps())
    hb = json.loads((Path(c.state_dir) / "pass.json").read_text())
    assert hb["interval_minutes"] == 5


def test_example_config_comments_and_still_loads(tmp_path, monkeypatch):
    text = (ROOT / "targets.example.yaml").read_text()
    assert "codex_stall_after_seconds" not in text
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith("stall_after_seconds:"))
    block = []
    for ln in reversed(lines[:i]):
        if not ln.startswith("#"):
            break
        block.append(ln)
    comment = " ".join(block).lower()
    assert "hang" in comment and "poke" in comment and "every pass" in comment
    pi = next(ln for ln in lines if re.match(r"#?\s*pass_interval_minutes:", ln))
    assert "5" in pi and "systemd" in pi and "10" not in pi
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    assert load_config(ROOT / "targets.example.yaml").pass_interval_minutes == 5


def test_codex_design_note_states_herdr_detection():
    text = (ROOT / "docs/specs/2026-09-24-codex-runtime-design.md").read_text()
    assert "HERDR_AGENT=codex" in text and "agent_not_found" in text
    assert "herdr does not recognise" not in text
