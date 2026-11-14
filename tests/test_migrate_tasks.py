"""`agent-ops migrate-tasks` (dispatcher.onboard_cli) under the pass lock, and
the dispatcher that refuses to start a pass until it has run."""
import json
import sys
from pathlib import Path

import pytest

from dispatcher import main, onboard_cli
from dispatcher.convergence import pass_lock
from dispatcher.migrate import migrate_state_dir, pending
from tests.test_main import cfg
from tests.test_migrate import contents, legacy_file, write
from tests.test_onboard import box, state  # noqa: F401
from tests.test_onboard_cli_rerun import as_agent  # noqa: F401


def migrate_tasks(box, *args):
    return onboard_cli.main(["migrate-tasks", *args, "--config", str(box)])


def task_files(sd):
    """Every file but the pass lock, which taking the lock creates."""
    return {k: v for k, v in contents(sd).items() if k != "convergence.lock"}


def legacy_dir(tmp_path, n=2):
    for issue in range(1, n + 1):
        write(state(tmp_path), legacy_file(issue=issue))
    return state(tmp_path)


def test_the_updater_runs_it_inside_the_lock_it_holds(tmp_path, box, as_agent, capsys):
    sd = legacy_dir(tmp_path)
    with pass_lock(str(sd)):   # a second flock would fail at once: none is taken
        assert migrate_tasks(box, "--lock-held") == 0
    assert pending(sd) == []
    assert "rewrote 2 task file(s)" in capsys.readouterr().out


def test_by_hand_it_takes_the_lock_and_rewrites(tmp_path, box, as_agent, capsys):
    sd = legacy_dir(tmp_path)
    assert migrate_tasks(box) == 0
    assert pending(sd) == []
    assert migrate_tasks(box) == 0
    assert "rewrote 0 task file(s)" in capsys.readouterr().out


def test_it_refuses_a_running_dispatcher(tmp_path, box, as_agent, capsys):
    sd = legacy_dir(tmp_path)
    before = task_files(sd)
    with pass_lock(str(sd)):
        assert migrate_tasks(box) != 0
    assert "dispatcher is running" in capsys.readouterr().err
    assert task_files(sd) == before


def test_a_file_it_cannot_rewrite_is_named_and_nothing_changes(tmp_path, box, as_agent, capsys):
    sd = legacy_dir(tmp_path)
    bad = sd / "task-portfolio_eval-9.json"
    bad.write_text("{not json")
    before = task_files(sd)
    assert migrate_tasks(box) != 0
    assert str(bad) in capsys.readouterr().err
    assert task_files(sd) == before


def test_pending_names_old_shape_files_archived_ones_included(tmp_path):
    sd = legacy_dir(tmp_path, n=1)
    snap = write(sd, legacy_file(issue=7), "artifacts/abc/task.json")
    (sd / "task-portfolio_eval-9.json").write_text("{not json")   # the pass's unreadable guard owns it
    assert pending(sd) == [sd / "task-portfolio_eval-1.json", snap]


def run_dispatcher(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    monkeypatch.setattr(main, "load_config", lambda path: c)
    passes = []
    monkeypatch.setattr(main, "guarded_pass", lambda *a, **kw: passes.append(a))
    monkeypatch.setattr(sys, "argv", ["agent-ops-dispatcher", "--config", "t.yaml"])
    main.main()
    return c, passes


def test_the_dispatcher_waits_for_the_migration(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    p = write(Path(c.state_dir), legacy_file(issue=5))
    before = p.read_bytes()
    with pytest.raises(SystemExit) as exc:
        run_dispatcher(tmp_path, monkeypatch)
    assert exc.value.code != 0 and "agent-ops migrate-tasks" in str(exc.value.code)
    assert p.read_bytes() == before
    migrate_state_dir(c.state_dir)
    _, passes = run_dispatcher(tmp_path, monkeypatch)
    assert len(passes) == 1
    assert json.loads(p.read_text())["status"]
