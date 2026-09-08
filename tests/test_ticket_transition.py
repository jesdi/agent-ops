"""First red regression for ticket starts deferred by execution admission."""
import json

import pytest

from dispatcher import main
from dispatcher.state import Stage
from tests.test_main import FakeSessions, cfg, deps, make_task, patch_usage, write_tickets


@pytest.mark.parametrize("denied_passes", [1, 3], ids=["one-denial", "repeated-denials"])
def test_budget_recovery_starts_the_unworked_ticket_before_review(
    tmp_path, monkeypatch, denied_passes,
):
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 1 complete",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    patch_usage(monkeypatch, util=0.99)
    for _ in range(denied_passes):
        main.run_pass(config, dependencies)
    assert sessions.spawned == []

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)

    assert len(sessions.spawned) == 1
    issue, stage, _model, prompt = sessions.spawned[0]
    assert (issue, stage) == (42, "implement")
    assert ".agent/tickets/02-t2.md" in prompt
