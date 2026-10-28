"""Ticket 02: names `git check-ref-format --branch` rejects wait in Ready."""
import pytest

from dispatcher.state import load
from tests.test_claim_base_acceptance import box, gh_for, pings, run  # noqa: F401


@pytest.mark.parametrize("name", ["HEAD", "feat//cart", "feat/", "feat.", "a..b", ".x", "x.lock"])
def test_name_git_rejects_waits_and_reaches_no_command(box, name):
    c, origin = box
    gh = gh_for({42: f"Base: {name}\n"})
    _, n = run(c, gh)
    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert origin.calls == []
    assert len(pings(n, "42", "no valid branch")) == 1
