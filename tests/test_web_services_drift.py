"""`GET /api/services` flags a managed job whose timer on the box is not the one the installed
services config renders: a hand-edited unit file or a drop-in over it."""
from dispatcher.services_config import timer_unit
from tests.test_web_services_acceptance import user_timer
from tests.test_web_services_pause_acceptance import box, job

SWEEP = "agent-ops-sweep"


def sweep(tmp_path, text, **kw):
    unit = tmp_path / f"{SWEEP}.timer"
    unit.write_text(text)
    t = user_timer("agent-ops-worktree-sweep", **kw)
    t.update(unit=f"{SWEEP}.timer", fragment_path=str(unit))
    return t


def test_a_timer_rendered_from_the_config_has_no_warning(tmp_path):
    _, client = box(tmp_path, [sweep(tmp_path, timer_unit("sweep", {"every": 5}))])
    assert job(client, SWEEP)["warning"] is None


def test_a_hand_edited_schedule_while_active_has_a_warning(tmp_path):
    _, client = box(tmp_path, [sweep(tmp_path, timer_unit("sweep", {"daily": "06:00", "tz": "UTC"}))])
    j = job(client, SWEEP)
    assert j["state"] == "active" and "config" in j["warning"]


def test_a_drop_in_over_the_timer_has_a_warning(tmp_path):
    t = sweep(tmp_path, timer_unit("sweep", {"every": 5}), drop_ins=["/home/agent/.config/x.conf"])
    _, client = box(tmp_path, [t])
    assert "config" in job(client, SWEEP)["warning"]


def test_a_console_schedule_change_still_applying_has_no_warning(tmp_path):
    t = sweep(tmp_path, timer_unit("sweep", {"daily": "06:00", "tz": "UTC"}))
    _, client = box(tmp_path, [t], changes={"sweep": {"schedule": {"every": 10}}})
    assert job(client, SWEEP)["warning"] is None
