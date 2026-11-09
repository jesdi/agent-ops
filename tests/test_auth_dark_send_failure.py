"""A failed auth_dark send (Notifier.send returns 0) must not consume the alert."""
import dispatcher.main as main
from tests.test_main import _ago, _dark_pass, _marker, _realert_h


def _fail_then_recover(tmp_path, monkeypatch, marker):
    c, d = _dark_pass(tmp_path, monkeypatch, marker)
    before = _marker(c)
    d.notifier.msg_id = 0
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 1
    assert _marker(c) == before
    d.notifier.msg_id = 77
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 2
    assert _marker(c)["last_alert"] != before.get("last_alert")


def test_failed_reminder_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    _fail_then_recover(tmp_path, monkeypatch, {
        "since": _ago(hours=9), "alerted": True,
        "last_alert": _ago(seconds=_realert_h(minutes=1).total_seconds())})


def test_failed_first_alert_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    _fail_then_recover(tmp_path, monkeypatch, {
        "since": _ago(minutes=main.AUTH_DARK_GRACE_MINUTES + 1),
        "alerted": False})
