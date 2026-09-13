"""Usage doubles shared by dispatcher, triage and web tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from dispatcher.usage import SESSION, WEEK, ProviderUsage, Window


def session_usage(util=0.2, mins=120.0, source="oauth", week=0.0, fable=None,
                  now: datetime | None = None, provider="anthropic") -> ProviderUsage:
    """An Anthropic reading whose session window is `util` used and resets in
    `mins`; the weekly window is half elapsed (allowance ~0.6) and `week`
    used, so tests that only care about the session gate keep their meaning."""
    now = now or datetime.now(timezone.utc)
    ws = [Window("session", None, util, now + timedelta(minutes=mins), SESSION),
          Window("weekly", None, week, now + timedelta(days=3.5), WEEK)]
    if fable is not None:
        ws.append(Window("weekly", "Fable", fable, now + timedelta(days=3.5), WEEK))
    return ProviderUsage(provider, source, time.time(), tuple(ws))


class FakeUsage:
    name = "fake"

    def __init__(self, result: ProviderUsage | None = None):
        self.result = result or session_usage(provider="fake")
        self.calls = 0

    def fetch(self, state_dir: Path, *, now=time.time) -> ProviderUsage:
        self.calls += 1
        return self.result
