"""`GET /api/services` flags a managed job whose loaded timer fires on another schedule than
the installed services config's: a hand edit picked up by a daemon-reload, or a drop-in."""
from dispatcher import services_config
from tests.test_web_services_acceptance import user_timer
from tests.test_web_services_pause_acceptance import box, job

SWEEP = "agent-ops-sweep"  # every 5 min in the installed config
DIGEST = "agent-ops-digest"  # daily at 08:00 UTC


def loaded(job_id, calendar=(), every_seconds=None, **kw):
    t = user_timer("agent-ops-digest", **kw)
    t.update(unit=f"{job_id}.timer", triggers={"calendar": list(calendar), "every_seconds": every_seconds})
    return t


def warning(tmp_path, timer, **kw):
    _, client = box(tmp_path, [timer], **kw)
    return job(client, timer["unit"].removesuffix(".timer"))["warning"]


def test_a_timer_firing_on_the_configured_interval_has_no_warning(tmp_path):
    assert warning(tmp_path, loaded(SWEEP, every_seconds=300)) is None


def test_a_loaded_interval_other_than_the_config_has_a_warning(tmp_path):
    assert "config" in warning(tmp_path, loaded(SWEEP, every_seconds=600))


def test_a_loaded_calendar_on_an_interval_job_has_a_warning(tmp_path):
    assert "config" in warning(tmp_path, loaded(SWEEP, ["*-*-* 06:00:00 UTC"], every_seconds=300))


def test_a_calendar_spelled_as_systemd_normalises_it_has_no_warning(tmp_path, monkeypatch):
    runs = {"*-*-* 08:00:00 UTC": ["2026-11-26T08:00:00+00:00"],
            "*-*-* 08:00:00": ["2026-11-26T08:00:00+00:00"]}
    monkeypatch.setattr(services_config, "calendar_runs", lambda cal, n=3: runs.get(cal, []))
    assert warning(tmp_path, loaded(DIGEST, ["*-*-* 08:00:00"])) is None


def test_a_loaded_calendar_firing_at_another_time_has_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(services_config, "calendar_runs", lambda cal, n=3: [cal])
    assert "config" in warning(tmp_path, loaded(DIGEST, ["*-*-* 06:00:00 UTC"]))


def test_a_drop_in_adding_a_second_calendar_has_a_warning(tmp_path):
    assert "config" in warning(tmp_path, loaded(DIGEST, ["*-*-* 08:00:00 UTC", "*-*-* 20:00:00 UTC"]))


def test_a_probe_reporting_no_triggers_tells_nothing(tmp_path):
    t = loaded(SWEEP)
    del t["triggers"]
    assert warning(tmp_path, t) is None


def test_a_console_schedule_change_still_applying_has_no_warning(tmp_path):
    t = loaded(SWEEP, every_seconds=600)
    assert warning(tmp_path, t, changes={"sweep": {"schedule": {"every": 10}}}) is None
