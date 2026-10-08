"""Shape checks of the review page beyond the acceptance tests (ticket 01)."""
from tests.test_main import FakeSessions, deps  # noqa: F401
from tests.test_plan_review_gate_acceptance import _plan_task, _setup
from tests.test_review_page_gate_acceptance import FIXTURE, KIB, PAGE, _bounced, _pass, _padded


def test_symlink_inside_the_worktree_does_not_enter_the_gate(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)
    (wt / ".agent" / "real.html").write_text(FIXTURE.read_text())
    (wt / PAGE).symlink_to(wt / ".agent" / "real.html")

    sess = _pass(c, wt)

    _bounced(c, sess)


def test_page_above_1_mib_is_bounced_for_its_size(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)

    sess = _pass(c, wt, _padded(2 * 1024 * KIB))

    assert "exceeds 256 KiB" in _bounced(c, sess)
