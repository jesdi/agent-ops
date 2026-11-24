"""In-process coverage of dispatcher.services_config (the CLI tests run in a subprocess)."""
import pytest
import yaml

from dispatcher import services_config as sc

JOBS = sc.JOBS


def cfg(**over):
    c = {j: {"schedule": {"every": 5}, "paused": False} for j in JOBS}
    c.update(over)
    return c


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
    return str(p)


@pytest.fixture(autouse=True)
def calendar_ok(monkeypatch):
    class R:
        def __init__(self, rc):
            self.returncode = rc
    monkeypatch.setattr(sc.subprocess, "run", lambda cmd, **k: R(1 if "bad" in cmd[-1] else 0))


@pytest.mark.parametrize("sched", [
    {"every": 5}, {"daily": "08:00", "tz": "UTC"}, {"calendar": "Mon *-*-* 09:00"}])
def test_valid(sched):
    sc._check_schedule("j", sched)


@pytest.mark.parametrize("sched", [
    "x", {}, {"every": 5, "calendar": "x"}, {"every": 5, "tz": "UTC"}, {"every": 0}, {"every": True},
    {"daily": "8:00", "tz": "UTC"}, {"daily": "08:00"}, {"daily": "08:00", "tz": "/UTC"},
    {"daily": "08:00", "tz": "No/Such_Zone"}, {"calendar": ""}, {"calendar": "-x"},
    {"calendar": "a\nb"}, {"calendar": 5}, {"calendar": "bad"}])
def test_invalid(sched):
    with pytest.raises(ValueError):
        sc._check_schedule("j", sched)


@pytest.mark.parametrize("bad", [
    "[]", {"bogus": 1}, cfg(dispatcher=None), cfg(dispatcher={"schedule": {"every": 5}})])
def test_load_refuses(tmp_path, bad):
    with pytest.raises(ValueError):
        sc.load(write(tmp_path, "c.yaml", bad))


def test_render_and_main(tmp_path, capsys):
    c = cfg(digest={"schedule": {"daily": "09:00", "tz": "UTC"}, "paused": True},
            triage={"schedule": {"calendar": "daily"}, "paused": False})
    units = tmp_path / "u"
    units.mkdir()
    args = [write(tmp_path, "c.yaml", c), write(tmp_path, "ch.yaml", {"triage": {"paused": True}}), str(units)]
    out = sc.render(*args)
    assert {"agent-ops-digest.timer", "agent-ops-triage.timer"} <= set(out["disabled"])
    assert "OnCalendar=*-*-* 09:00:00 UTC" in (units / "agent-ops-digest.timer").read_text()
    assert "OnCalendar=daily" in (units / "agent-ops-triage.timer").read_text()
    assert sc.main(["render", *args]) == 0
    assert sc.main(["validate", args[0]]) == 0
    assert sc.main([]) == 2
    assert sc.main(["validate", str(tmp_path / "missing")]) == 1
    with pytest.raises(ValueError):
        sc.render(args[0], write(tmp_path, "x.yaml", "[1]"), str(units))
    capsys.readouterr()
