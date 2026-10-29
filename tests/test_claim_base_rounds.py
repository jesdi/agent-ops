"""Ticket 02: base waits are per repository, and a body read failure costs one candidate."""
import json
import subprocess

import pytest
from dataclasses import replace as dc_replace

from dispatcher.github import Candidate
from dispatcher.state import load

from tests.test_claim_base_acceptance import BodyGitHub, Origin, pings, run
from tests.test_main import FakeNotifier, cfg, patch_usage
from tests.test_multi_project_capacity import two_repository_cfg


def test_same_issue_waiting_in_two_repositories_pings_twice(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    Origin(monkeypatch)
    c = two_repository_cfg(tmp_path)
    gh = BodyGitHub({42: "Base: feat/new-cart\n"}, cands_by_repository={
        t.name: [Candidate(42, "t42", f"u-{t.name}")] for t in c.repositories})
    n = FakeNotifier()
    run(c, gh, n)
    run(c, gh, n)
    assert len(pings(n, "42", "feat/new-cart")) == 2


class FlakyBodyGitHub(BodyGitHub):
    def __init__(self, bodies, failing, error=None, **kw):
        super().__init__(bodies, **kw)
        self.failing = set(failing)
        self.error = error or subprocess.TimeoutExpired(["gh", "issue", "view"], 30)

    def issue_body(self, repository, issue):
        if issue in self.failing:
            raise self.error
        return super().issue_body(repository, issue)


@pytest.mark.parametrize("error", [None, json.JSONDecodeError("Expecting value", "", 0)])
def test_body_read_failure_skips_only_that_candidate(tmp_path, monkeypatch, error):
    patch_usage(monkeypatch)
    Origin(monkeypatch)
    c = dc_replace(cfg(tmp_path), capacity=1)
    gh = FlakyBodyGitHub({}, failing={42}, error=error,
                         cands=[Candidate(i, f"t{i}", f"u{i}") for i in (42, 44)])
    run(c, gh)
    assert gh.claimed == [44]
    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert gh.released == []
