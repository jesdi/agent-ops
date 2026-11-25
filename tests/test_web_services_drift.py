"""`GET /api/services` flags a managed job whose loaded timer fires on another schedule than
the installed services config's: a hand edit picked up by a daemon-reload, or a drop-in."""
from types import SimpleNamespace

from dispatcher import services_config
from tests.test_web_services_acceptance import user_timer
from tests.test_web_services_pause_acceptance import box, job

SWEEP = "agent-ops-sweep"  # every 5 min in the installed config
DIGEST = "agent-ops-digest"  # daily at 08:00 UTC


def loaded(job_id, calendar=(), every_seconds=None, **kw):
    t = user_timer("agent-ops-digest", **kw)
    t.update(unit=f"{job_id}.timer", triggers={"calendar": list(calendar), "every_seconds": every_seconds})
    return t


# Monday 2026-11-30 before 08:00 UTC: daily and weekdays at 08:00 agree on the next three runs.
AT_8 = ("Mon 2026-11-30 08:00:00 UTC", "Tue 2026-12-01 08:00:00 UTC", "Wed 2026-12-02 08:00:00 UTC")
AT_6 = tuple(r.replace("08:00", "06:00") for r in AT_8)
ANALYZED = {  # expression -> (normalized form, next three runs)
    "*-*-* 08:00:00 UTC": ("*-*-* 08:00:00 UTC", AT_8), "*-*-* 08:00 UTC": ("*-*-* 08:00:00 UTC", AT_8),
    "Mon..Fri *-*-* 08:00:00 UTC": ("Mon..Fri *-*-* 08:00:00 UTC", AT_8),
    "*-*-* 06:00:00 UTC": ("*-*-* 06:00:00 UTC", AT_6)}


def analyze(monkeypatch):
    """A fake `systemd-analyze calendar [--iterations=N] <expr>`, printing what ANALYZED holds
    for the expression; an unknown expression fails as systemd would."""
    def run(cmd, **kw):
        if cmd[-1] not in ANALYZED:
            return SimpleNamespace(returncode=1, stdout="", stderr="bad")
        norm, runs = ANALYZED[cmd[-1]]
        iters = "".join(f"       Iter. #{i}: {r}\n" for i, r in enumerate(runs[1:], 2))
        return SimpleNamespace(returncode=0, stderr="", stdout=(
            f"  Original form: {cmd[-1]}\nNormalized form: {norm}\n    Next elapse: {runs[0]}\n{iters}"))
    monkeypatch.setattr(services_config.subprocess, "run", run)


def warning(tmp_path, timer, **kw):
    _, client = box(tmp_path, [timer], **kw)
    return job(client, timer["unit"].removesuffix(".timer"))["warning"]


def test_a_timer_firing_on_the_configured_interval_has_no_warning(tmp_path):
    assert warning(tmp_path, loaded(SWEEP, every_seconds=300)) is None


def test_a_loaded_interval_other_than_the_config_has_a_warning(tmp_path):
    assert warning(tmp_path, loaded(SWEEP, every_seconds=600)) == (
        "The timer fires every 10 min; the services config says every 5 min.")


def test_a_loaded_calendar_on_an_interval_job_has_a_warning(tmp_path):
    assert warning(tmp_path, loaded(SWEEP, ["*-*-* 06:00:00 UTC"], every_seconds=300)) == (
        "The timer fires daily at 06:00 UTC and every 5 min; the services config says every 5 min.")


def test_a_calendar_spelled_another_way_for_the_same_schedule_has_no_warning(tmp_path, monkeypatch):
    analyze(monkeypatch)
    assert warning(tmp_path, loaded(DIGEST, ["*-*-* 08:00 UTC"])) is None


def test_a_loaded_calendar_firing_at_another_time_has_a_warning(tmp_path, monkeypatch):
    analyze(monkeypatch)
    assert warning(tmp_path, loaded(DIGEST, ["*-*-* 06:00:00 UTC"])) == (
        "The timer fires daily at 06:00 UTC; the services config says daily at 08:00 UTC.")


def test_a_loaded_calendar_agreeing_on_the_next_runs_but_not_the_schedule_has_a_warning(tmp_path, monkeypatch):
    """Daily vs weekdays at 08:00 share their next three runs before Monday 08:00."""
    analyze(monkeypatch)
    assert warning(tmp_path, loaded(DIGEST, ["Mon..Fri *-*-* 08:00:00 UTC"])) == (
        "The timer fires on Mon..Fri *-*-* 08:00:00 UTC; the services config says daily at 08:00 UTC.")


def test_a_drop_in_adding_a_second_calendar_has_a_warning(tmp_path):
    assert warning(tmp_path, loaded(DIGEST, ["*-*-* 08:00:00 UTC", "*-*-* 20:00:00 UTC"])) == (
        "The timer fires daily at 08:00 UTC and daily at 20:00 UTC; the services config says daily at 08:00 UTC.")


def test_a_probe_reporting_no_triggers_tells_nothing(tmp_path):
    t = loaded(SWEEP)
    del t["triggers"]
    assert warning(tmp_path, t) is None


def test_a_console_schedule_change_still_applying_has_no_warning(tmp_path):
    t = loaded(SWEEP, every_seconds=600)
    assert warning(tmp_path, t, changes={"sweep": {"schedule": {"every": 10}}}) is None


def test_a_timer_switched_off_by_hand_on_another_schedule_says_both(tmp_path):
    assert warning(tmp_path, loaded(SWEEP, every_seconds=600, active_state="inactive")) == (
        "Paused on the box but not in the services config. "
        "The timer fires every 10 min; the services config says every 5 min.")
