"""Helpers of the pinned-tracks tests that are not acceptance files: one
import for the spec's example policy, usage and ticket-set fixtures, and one
definition of each helper those tests share.

The fixtures in the first block are defined in the acceptance files, which
stay as they were written; they are re-exported here so that no other test
module imports from a test module. A ticket names no track of its own: the
helpers write plain ticket files."""
import json
from dataclasses import replace

import dispatcher.main as main
from dispatcher import intents, state
from dispatcher.state import PARK_WAKE, Stage, TaskState, load, save
from tests.test_main import (GOOD_TICKET, FakeGitHub, FakeSessions, deps,
                             make_task)
from tests.test_pinned_tracks_order import (ASTRA, FABLE, OPUS, SOL,
                                            SONNET, ahead, deny, enter,
                                            launched, make_cfg, policy,
                                            policy_raw)
from tests.test_web_pinned_tracks import (HEADERS, anthropic,
                                          cards, detail, models, openai, rig)
from tests.test_web_pinned_tracks import policy as web_policy

PLAN_DONE = {"stage": "plan", "status": "done", "note": "tickets",
             "artifact": ".agent/tickets"}
IMPL_DONE = {"stage": "implement", "status": "done"}
BOTH = ["openai", "anthropic"]
DENY_FRONTEND = (FABLE, OPUS)                # its implement list
DENY_ARCHITECTURE = ("gpt-astra", FABLE)     # its implement and review lists


def task(stage=Stage.IMPLEMENT, track="architecture", **kw):
    """A task that exists only in memory."""
    return TaskState(issue=42, target="t", stage=stage, slot=1, worktree="",
                     branch="b", title="", updated_at="u", track=track, **kw)


def saved(c, issue=42):
    return load(c.state_dir, "portfolio_eval", issue)


def raw(c, issue=42):
    """The state file as written: `saved` goes through the read migrations."""
    return json.loads(
        state._path(c.state_dir, "portfolio_eval", issue).read_text())


def rewrite(state_dir, target, *drop, **over):
    """Issue 42's state file without the keys `drop` and with `over`: the
    file as older code wrote it."""
    p = state._path(state_dir, target, 42)
    doc = json.loads(p.read_text())
    p.write_text(json.dumps(
        {**{k: v for k, v in doc.items() if k not in drop}, **over}))
    return p


def old_shape(state_dir, stage, picks, **raw_over):
    """A state file with no recorded provider, as before the record existed."""
    save(state_dir, task(stage, picks=picks))
    return rewrite(state_dir, "t", "implement_providers", **raw_over)


def write_ticket(wt, n):
    d = wt / ".agent" / "tickets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{n:02d}-t{n}.md").write_text(GOOD_TICKET)


def setup(tmp_path, monkeypatch, task_track, tickets, usages=None, **kw):
    """A task at the plan review gate whose plan session wrote `tickets`
    ticket files: PLAN_DONE is the operator's approval."""
    c = make_cfg(tmp_path, monkeypatch, usages or ahead(), **kw)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_PLAN_REVIEW,
                   track=task_track)
    for n in range(1, tickets + 1):
        write_ticket(wt, n)
    return c, wt


def usage_now(monkeypatch, usages):
    monkeypatch.setattr(main, "fetch_all", lambda cfg, **k: usages)


def step(c, wt, signal, alive=True):
    (wt / ".agent" / "stage.json").write_text(json.dumps(signal))
    sess = FakeSessions(alive={42} if alive else set())
    main.run_pass(c, deps(sess=sess))
    return sess


def unpinned(c):
    """The config after `frontend` left the pinned list."""
    return replace(c, models=policy(pinned=["security", "architecture"]))


def wake(c, cfg=None, **kw):
    """The operator wakes the task; one pass (of `cfg`, when the config
    changed since)."""
    save(c.state_dir, replace(saved(c), park=PARK_WAKE, **kw))
    sess = FakeSessions()
    main.run_pass(cfg or c, deps(sess=sess))
    return sess


def resume_intent(c, sess=None, **payload):
    intents.write_intent(c.state_dir, "resume", "portfolio_eval", 42, payload,
                         "op", 1)
    sess = sess or FakeSessions()
    main.run_pass(c, deps(FakeGitHub(), sess))
    return sess
