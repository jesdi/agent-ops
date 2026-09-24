"""The dispatcher's claim round, end to end through run_pass."""
import dispatcher.main as main
from dispatcher.github import Candidate

from tests.test_main import FakeGitHub, FakeSessions, deps, patch_usage, patch_workspace
from tests.test_multi_project_capacity import mk_task, two_target_cfg


def test_rank_runs_only_for_targets_that_get_a_turn(tmp_path, monkeypatch):
    """capacity 3, factorial at 2 active, portfolio_eval at 0: the one free
    unit goes to portfolio_eval, and factorial's candidates() (its rank_cmd)
    never runs — eagerly ranking every target would call it."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)

    class CountingGitHub(FakeGitHub):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.candidates_calls = []

        def candidates(self, target):
            self.candidates_calls.append(target.name)
            return super().candidates(target)

    c = two_target_cfg(tmp_path)
    mk_task(c, "factorial", 1, slot=0)
    mk_task(c, "factorial", 2, slot=1)
    gh = CountingGitHub(cands_by_target={
        "portfolio_eval": [Candidate(10, "PE candidate", "u10")],
        "factorial": [Candidate(20, "F candidate", "u20")],
    })
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2})))
    assert gh.claimed == [10]
    assert "factorial" not in gh.candidates_calls
