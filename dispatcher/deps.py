"""The dispatcher's injected collaborators (Deps), the budget gate's shape
(Admit), and the pass's clock readings."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from dispatcher import github_app, task_tokens
from dispatcher.usage import Verdict

Admit = Callable[[str], Verdict]


@dataclass
class Deps:
    github: object
    sessions: object
    notifier: object
    # The GitHub App: every container's token comes from it. Required, so no
    # launch ever falls back to the box's stored all-repos PAT.
    app: github_app.Client
    # the time a task token's life is measured against
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    # The pass's App access (_begin_pass_auth): one cache for every token the pass mints.
    access: github_app.Access = field(init=False)

    def __post_init__(self):
        self.access = _access(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cursor_now() -> str:
    """Now, truncated to whole seconds and stepped back one, for
    feedback_cursor only (same ISO shape as _now() otherwise).

    GitHub reports comment/review timestamps at second granularity: a
    comment posted at 10:00:00.9 comes back as "10:00:00Z". pr_poll's
    trigger is a strict `dt > cursor`, so a cursor of 10:00:00.5 — or of
    10:00:00, truncation alone changes nothing here — hides that comment
    from every future round even though it was posted AFTER the session
    that was supposed to see it started. Whole seconds minus one make the
    whole second the spawn happened in still readable, which is the side
    the spec picks: a redundant round beats a lost comment."""
    return (datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(seconds=1)).isoformat()


def _access(deps: Deps) -> github_app.Access:
    return github_app.Access(deps.app, now=deps.clock, refresh=task_tokens.REFRESH)
