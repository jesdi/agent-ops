"""External process teardown contract for the locked T3 test harness."""
import signal

import pytest

from tests import test_bound_turns_t3_acceptance as harness


class ReapedSupervisor:
    pid = 732001

    def poll(self):
        return 70


def test_reaped_group_permission_race_requires_confirmed_group_disappearance(monkeypatch):
    calls = []
    statuses = [PermissionError("Darwin reaped group race"), PermissionError("still transient"),
                ProcessLookupError("owned group disappeared")]

    def signal_group(pid, number):
        calls.append((pid, number))
        raise statuses.pop(0)

    monkeypatch.setattr(harness.os, "killpg", signal_group)
    harness.terminate(ReapedSupervisor())
    assert calls == [(732001, signal.SIGKILL), (732001, 0), (732001, 0)], (
        "after EPERM, only nonmutating checks of the same owned group may confirm its disappearance")


@pytest.mark.parametrize("group_status", ["permission-denied", "still-exists"])
def test_group_permission_failure_remains_visible_without_confirmed_disappearance(monkeypatch, group_status):
    original = PermissionError("owned group cleanup denied")
    calls = []

    def signal_group(pid, number):
        calls.append((pid, number))
        if len(calls) == 1 or group_status == "permission-denied":
            raise original
        return None

    monkeypatch.setattr(harness.os, "killpg", signal_group)
    with pytest.raises(PermissionError) as failure:
        harness.terminate(ReapedSupervisor())
    assert failure.value is original
    assert calls[0] == (732001, signal.SIGKILL)
    assert all(call == (732001, 0) for call in calls[1:])
