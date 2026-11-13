"""The dispatcher's JSON state and settings writes go through the one atomic
writer: same bytes as before, mode 0644 whatever the umask, no temp file."""
import dataclasses
import json
import os
from contextlib import contextmanager

import pytest

from dispatcher import base_line, execution_overrides, intents, main, state, triage
from dispatcher.config import Config
from dispatcher.state import Stage, TaskState

ISSUE = 149
STAMP = "2026-11-12T10:00:00+00:00"


@contextmanager
def umask(mask):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def files(root):
    return {p for p in root.rglob("*") if p.is_file()}


def only_new(root, before):
    [p] = sorted(files(root) - before - set(root.rglob("*.lock")))
    return p


def task(**kw):
    return TaskState(issue=ISSUE, repository="portfolio_eval", stage=Stage.IMPLEMENT,
                     slot=0, worktree="/w/task-149", branch="agent/task-149",
                     title="Add widget", updated_at=STAMP, **kw)


def task_bytes(ts):
    d = dataclasses.asdict(ts)
    d["stage"] = ts.stage.value
    return json.dumps(d, indent=2).encode()


def record_wait(sd):
    before = files(sd)
    base_line.record_wait(str(sd), "repo.name", ISSUE, "k")
    return only_new(sd, before), b'{"key": "k"}'


def exec_override(sd):
    before = files(sd)
    execution_overrides.save(sd, "portfolio_eval", 42,
                             execution_overrides.ExecutionOverride("opus", True))
    return (only_new(sd, before),
            b'{\n  "model": "opus",\n  "bypass_usage": true\n}')


def queue_write(sd):
    q = intents.Queue("things", ("approve",), ("repository", "issue"))
    p = q.write(sd, "approve", ("portfolio_eval", 7), {"n": 1}, "jesdi@github",
                1753430000000)
    created = json.loads(p.read_bytes())["created_at"]
    return p, ('{\n  "action": "approve",\n  "repository": "portfolio_eval",\n'
               '  "issue": 7,\n  "payload": {\n    "n": 1\n  },\n'
               f'  "actor": "jesdi@github",\n  "created_at": "{created}"\n}}').encode()


def heartbeat(sd):
    cfg = Config(state_dir=str(sd), capacity=1, session_memory="2g", session_cpus="2",
                 repositories=[], pass_interval_minutes=5)
    main._write_heartbeat(cfg, STAMP)
    p = sd / "pass.json"
    finished = json.loads(p.read_bytes())["finished_at"]
    return p, (f'{{"started_at": "{STAMP}", "finished_at": "{finished}", '
               '"interval_minutes": 5}').encode()


def task_save(sd):
    ts = task()
    state.save(sd, ts)
    return sd / "task-portfolio_eval-149.json", task_bytes(ts)


def task_archive(sd):
    ts = dataclasses.replace(task(), stage=Stage.DONE, terminal_at=STAMP)
    state.archive(sd, ts)
    return (state.archive_root(sd, ts.repository, ts.issue) / "task.json",
            task_bytes(ts))


def background(sd):
    before = files(sd)
    state.mark_background(sd, "portfolio_eval", ISSUE, [{"id": "b1"}], now=1000.0)
    return (only_new(sd, before),
            b'{"tasks": ["b1"], "since": 1000.0, "reported": 1000.0}')


def triage_enqueue(sd):
    before = files(sd)
    assert triage.enqueue(sd) is True
    p = only_new(sd, before)
    requested = json.loads(p.read_bytes())["requested_at"]
    return p, f'{{\n  "requested_at": "{requested}"\n}}'.encode()


def cursors(sd):
    before = files(sd)
    triage.save_cursors(sd, {"z": "2026-01-02T00:00:00+00:00",
                             "a": "2026-01-01T00:00:00+00:00"})
    return (only_new(sd, before),
            b'{\n  "a": "2026-01-01T00:00:00+00:00",\n'
            b'  "z": "2026-01-02T00:00:00+00:00"\n}')


SITES = [record_wait, exec_override, queue_write, heartbeat, task_save, task_archive,
         background, triage_enqueue, cursors]


@pytest.mark.parametrize("site", SITES, ids=lambda f: f.__name__)
def test_a_site_keeps_its_bytes_and_ends_0644_without_a_temp_file(tmp_path, site):
    sd = tmp_path / "state"
    with umask(0o077):
        p, expected = site(sd)
    assert p.read_bytes() == expected
    assert list(sd.rglob("*.tmp")) == []
    assert p.stat().st_mode & 0o777 == 0o644


def test_save_cursors_writes_sorted_keys_indented_without_a_trailing_newline(tmp_path):
    sd = tmp_path / "state"
    with umask(0o077):
        triage.save_cursors(sd, {"z": "…", "a": "…"})
    [p] = [p for p in sd.iterdir() if p.suffix == ".json"]
    assert p.read_bytes() == b'{\n  "a": "\\u2026",\n  "z": "\\u2026"\n}'  # ensure_ascii
    assert list(sd.glob("*.tmp")) == []
    assert p.stat().st_mode & 0o777 == 0o644


def test_a_dotted_repository_background_file_reads_back_with_no_temp_file(tmp_path):
    sd = tmp_path / "state"
    with umask(0o077):
        state.mark_background(sd, "owner/repo.name", ISSUE, [{"id": "b1"}], now=1000.0)
    bg = state.read_background(sd, "owner/repo.name", ISSUE)
    assert bg == state.BackgroundWait(("b1",), 1000.0, 1000.0)
    [p] = files(sd)
    assert p.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in p.parent.iterdir()] == [p.name]


def test_a_heartbeat_that_cannot_be_written_warns_and_leaves_no_temp_file(tmp_path, capsys):
    sd = tmp_path / "state"
    (sd / "pass.json").mkdir(parents=True)
    cfg = Config(state_dir=str(sd), capacity=1, session_memory="2g",
                 session_cpus="2", repositories=[])
    main._write_heartbeat(cfg, STAMP)
    assert "[warn] heartbeat write failed:" in capsys.readouterr().err
    assert [p.name for p in sd.iterdir()] == ["pass.json"]
    assert (sd / "pass.json").is_dir()


def test_mark_background_removes_the_waiting_marker_before_it_writes(tmp_path, monkeypatch):
    sd = tmp_path / "state"
    state.mark_waiting(sd, "portfolio_eval", ISSUE)
    assert state.has_waiting(sd, "portfolio_eval", ISSUE)
    seen = []
    real = state.write_json_atomic

    def spy(path, data, **kw):
        seen.append(state.has_waiting(sd, "portfolio_eval", ISSUE))
        return real(path, data, **kw)

    monkeypatch.setattr(state, "write_json_atomic", spy)
    state.mark_background(sd, "portfolio_eval", ISSUE, [{"id": "b1"}], now=1000.0)
    assert seen == [False]
    assert state.read_background(sd, "portfolio_eval", ISSUE) is not None
