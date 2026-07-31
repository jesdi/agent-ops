"""Content assertions on the provisioned systemd user units. The units are
plain files synced verbatim by update.sh, so these tests pin the properties
the specs mandate (cadence, failure wiring) against silent regression."""
from pathlib import Path

PROVISION = Path(__file__).resolve().parent.parent / "provision"


def unit_text(name: str) -> str:
    return (PROVISION / name).read_text()


def test_keepalive_timer_is_hourly():
    # Spec 2026-07-31-auth-resilience: daily cadence let the refresh token
    # lapse past recovery; hourly keeps it inside its refresh window.
    text = unit_text("agent-ops-keepalive.timer")
    assert "OnCalendar=hourly" in text
    assert "OnCalendar=daily" not in text
    assert "Persistent=true" in text


def test_keepalive_failure_triggers_alert_unit():
    assert ("OnFailure=agent-ops-alert@%n.service"
            in unit_text("agent-ops-keepalive.service"))


def test_alert_template_unit_runs_telegram_alert():
    text = unit_text("agent-ops-alert@.service")
    assert "telegram.alert %i" in text
    assert "op run" in text          # same secret plumbing as the dispatcher
    assert "Type=oneshot" in text
