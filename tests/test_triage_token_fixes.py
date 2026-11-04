"""Ticket 06 beyond the acceptance file: the sweep runs in its own process
(`--triage-run`, launched by `triage.tick` in a herdr tab), so that process must
get what it mints the triage token with, and start with the App wired."""
import sys
from pathlib import Path

import dispatcher.main as main
from dispatcher import github_app, task_tokens, triage
from tests.test_triage import FakeDeps, _herdr_fake_creating, _sweep_cfg


def test_tick_forwards_the_1password_token_the_runner_mints_with(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "running", lambda: False)
    for var in triage.LAUNCH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_secret")
    calls = []
    _herdr_fake_creating(monkeypatch, calls)
    triage.enqueue(tmp_path)
    triage.tick(_sweep_cfg(tmp_path), FakeDeps(), "targets.yaml")
    create = next(c for c in calls if c[:2] == ["tab", "create"])
    assert [create[i + 1] for i, a in enumerate(create) if a == "--env"] == [
        "OP_SERVICE_ACCOUNT_TOKEN=ops_secret"]
    run = next(c for c in calls if c[:2] == ["pane", "run"])
    assert "--triage-run" in run[3]


def test_the_triage_runner_sweeps_with_the_github_app_wired(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    monkeypatch.setattr(main, "_load", lambda path: (cfg, cfg))
    monkeypatch.setattr(sys, "argv", ["agent-ops-dispatcher", "--config", "t.yaml", "--triage-run"])
    swept = []
    monkeypatch.setattr(triage, "guarded_sweep", lambda cfg_, deps: swept.append(deps))
    main.main()
    [deps] = swept
    assert isinstance(deps.app, github_app.Client)


def test_a_sweep_starts_by_removing_a_killed_sweep_s_token_directories(tmp_path):
    leftover = Path(tmp_path) / task_tokens.TRIAGE / "triage-dead"
    leftover.mkdir(parents=True)
    (leftover / "hosts.yml").write_text("github.com:\n    oauth_token: ghs_leftover\n")
    task_tokens.prune_triage(tmp_path)
    assert not leftover.exists()
