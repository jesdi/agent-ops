"""Write verification of a confirmed operation: per-repository receipts, retry reuse, exact cleanup."""
from project_control import ProjectControl

FRONT, BACK = "example/front", "example/back"


class Github:
    def main(self):
        return "s1", {"projects": {}}


class Verifier:
    def __init__(self):
        self.deny, self.writes, self.cleaned, self.fail_cleanup = {BACK: "pr"}, [], [], False

    def authorize(self, repo):
        return dict(state="granted", action="")

    def write(self, repo, kind):
        if self.deny.get(repo) == kind:
            raise PermissionError(kind)
        self.writes.append((repo, kind))
        return f"{repo}:{kind}"

    def cleanup(self, repo, receipt):
        self.cleaned.append(receipt["id"])
        if self.fail_cleanup:
            return dict(state="failed", detail="locked")
        return dict(state="closed" if receipt["kind"] == "pr" else "removed", detail="")


def test_a_backend_denial_is_named_frontend_progress_is_kept_and_only_receipts_are_cleaned(tmp_path):
    v = Verifier()
    pc = ProjectControl(tmp_path, Github(), None, verifier=v)
    plan = dict(name="shop", revision="r", repositories=[dict(repo=FRONT), dict(repo=BACK)], board=None, models="inherit")
    op = pc.confirm(plan)
    first = pc.verify_writes(op)
    assert (first[FRONT]["state"], first[BACK]["state"], first[BACK]["denied"]) == ("passed", "failed", ["pr"])
    assert sorted(v.cleaned) == [f"{BACK}:branch", f"{FRONT}:branch", f"{FRONT}:pr"]
    v.deny = {}
    again = pc.verify_writes(op)
    assert again[BACK]["state"] == "passed" and v.writes.count((FRONT, "branch")) == 1
    assert v.writes.count((BACK, "branch")) == 2  # the cleaned branch is recreated for the retry's PR
    assert v.writes.count((BACK, "pr")) == 1


def test_a_failed_cleanup_is_reported_and_retried(tmp_path):
    v = Verifier()
    v.deny, v.fail_cleanup = {}, True
    pc = ProjectControl(tmp_path, Github(), None, verifier=v)
    op = pc.confirm(dict(name="a", revision="r", repositories=[dict(repo=FRONT)], board=None, models="inherit"))
    assert {c["state"] for c in pc.verify_writes(op)[FRONT]["cleanup"]} == {"failed"}
    v.fail_cleanup = False
    assert {c["state"] for c in pc.verify_writes(op)[FRONT]["cleanup"]} == {"removed", "closed"}


def test_a_lost_write_response_is_adopted_not_duplicated_and_a_cleanup_error_is_recorded(tmp_path):
    class Lossy(Verifier):
        lost = True

        def write(self, repo, kind):
            rid = super().write(repo, kind)
            if self.lost and kind == "branch":
                self.lost = False
                raise TimeoutError("response lost")
            return rid

        def lookup(self, repo, kind):
            return f"{repo}:{kind}"

        def cleanup(self, repo, receipt):
            if receipt["kind"] == "pr":
                raise TimeoutError("transport")
            return super().cleanup(repo, receipt)

    v = Lossy()
    v.deny = {}
    pc = ProjectControl(tmp_path, Github(), None, verifier=v)
    op = pc.confirm(dict(name="a", revision="r", repositories=[dict(repo=FRONT)], board=None, models="inherit"))
    try:
        pc.verify_writes(op)
    except TimeoutError:
        pass
    rec = pc.verify_writes(op)[FRONT]
    assert v.writes.count((FRONT, "branch")) == 1 and rec["state"] == "passed"
    assert [c["state"] for c in rec["cleanup"]] == ["removed", "failed"]


def onboarding(tmp_path, verifier):
    from tests.test_project_control_board_steps import Boards, plan
    from tests.test_project_control_pr_deployment_acceptance import Github as Infra, Deployer
    gh = Infra(tmp_path)
    pc = ProjectControl(tmp_path, gh, Deployer(tmp_path, gh), Boards(), verifier=verifier)
    op = pc.confirm(plan(dict(owner="example", number=7), prep=dict(fields=[], options=[], labels={})))
    return pc, op


def test_missing_verifier_blocks_real_onboarding_and_can_resume(tmp_path):
    pc, op = onboarding(tmp_path, None)
    pc.advance(op)
    assert pc.status(op)["phase"] == "blocked"
    assert "verifier" in pc.status(op)["blocker"]
    assert pc.status(op)["applied_revision"] is None
    pc.verifier = Verifier()
    pc.advance(op)
    assert pc.status(op)["phase"] == "complete"


def test_readable_board_requires_a_write_and_cleanup_before_completion(tmp_path):
    v = Verifier()
    v.deny = {"board:PVT_7": "board"}
    pc, op = onboarding(tmp_path, v)
    pc.advance(op)
    assert pc.status(op)["phase"] == "blocked"
    assert "board" in pc.status(op)["blocker"]
    assert pc.boards.applied == [] and pc.boards.imports == []
    v.deny = {}
    v.fail_cleanup = True
    pc.advance(op)
    assert pc.status(op)["phase"] == "blocked"
    v.fail_cleanup = False
    pc.advance(op)
    assert pc.status(op)["phase"] == "complete"
    assert v.writes.count(("example/analytics", "branch")) == 1
    assert v.writes.count(("board:PVT_7", "board")) == 1
    record = pc._load(op)["board_verification"]
    assert record["receipts"][0]["id"] == "board:PVT_7:board"
    assert record["cleanup"][0]["state"] == "removed"


def test_restart_between_cleanup_receipts_resumes_only_remaining_cleanup(tmp_path):
    import pytest

    class Interrupted(Verifier):
        interrupt = True

        def cleanup(self, repo, receipt):
            if receipt["kind"] == "pr" and self.interrupt:
                raise SystemExit("worker stopped")
            return super().cleanup(repo, receipt)

    v = Interrupted()
    pc = ProjectControl(tmp_path, Github(), None, verifier=v)
    op = pc.confirm(dict(name="a", revision="r", repositories=[dict(repo=FRONT)], board=None, models="inherit"))
    with pytest.raises(SystemExit):
        pc.verify_writes(op)
    v.interrupt = False
    restarted = ProjectControl(tmp_path, Github(), None, verifier=v)
    record = restarted.verify_writes(op)[FRONT]
    assert v.writes == [(FRONT, "branch"), (FRONT, "pr")]
    assert v.cleaned == [f"{FRONT}:branch", f"{FRONT}:pr"]
    assert len(record["cleanup"]) == 2
