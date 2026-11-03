"""Rebuild onboarding PRs after agent-ops updates (issue 253), through
onboard.run only: records and intents persist between runs, git and GitHub are
the fake `sh`. The executing checkout's revision is what `git rev-parse` answers
for the agent-ops checkout (FakeSh.builder)."""
import json
import os
from datetime import timedelta

import pytest

from dispatcher import onboard
from tests.test_onboard import (  # noqa: F401  (fixtures)
    NOW, NO_CI_RUN, PR_URL, FakeSh, add, box, confirmed, failure, infra_routes,
    infra_token, intent, pr_view, record, resolved, run, state, statuses, step)

A, B = "builder-a\n", "builder-b\n"
NO_RUNS = json.dumps({"workflow_runs": []})
T1 = NOW + timedelta(minutes=40)
T2 = NOW + timedelta(minutes=75)


def open_pr(head):
    return json.dumps([{"number": 7, "url": PR_URL, "headRefOid": head}])


def pr_run(tmp_path, box, builder, *, base="base1", owned="head1", new="head2",
           now=NOW, push=None, **extra):
    """One run with PR 7 open, the branch at `owned`, infra main at `base`; a
    build here would produce `new`. CI never shows up on any head."""
    routes = {f"actions/runs?head_sha={h}": NO_RUNS for h in (owned, new)}
    routes |= {f"commits/{h}/status": statuses() for h in (owned, new)}
    sh = FakeSh(infra_routes(open_prs=open_pr(owned), **routes, **extra))
    sh.builder = builder
    sh.route("rev-parse origin/main", base + "\n")
    sh.route("rev-parse HEAD", new + "\n")
    sh.route("for-each-ref", owned + "\n")
    if push is not None:
        sh.route(" push ", push)
    run(box, sh, now=now)
    return sh


def built_by(tmp_path, box, builder):
    """Confirmed and built (head1 on base1) by `builder`, PR 7 open."""
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    sh = FakeSh(infra_routes(**NO_CI_RUN))
    sh.builder = builder
    run(box, sh)
    return record(tmp_path)


def pr(tmp_path):
    return record(tmp_path)["infra_pr"]


def test_the_initial_build_records_its_builder_apart_from_proposal_and_base(
        tmp_path, box, infra_token):
    rec = built_by(tmp_path, box, A)
    assert rec["infra_pr"].get("builder") == A.strip()
    assert (rec["infra_pr"]["revision"], rec["infra_pr"]["base"]) == (rec["revision"], "base1")


def test_the_installed_builder_refreshes_the_open_pr_after_the_race(
        tmp_path, box, infra_token):
    """A builds on M1, infra main moves to M2, A rebuilds, then B is installed."""
    first = built_by(tmp_path, box, A)
    pr_run(tmp_path, box, A, base="base2", owned="head1", new="head2", now=T1)
    assert (pr(tmp_path)["head"], pr(tmp_path)["base"]) == ("head2", "base2")
    sh = pr_run(tmp_path, box, B, base="base2", owned="head2", new="head3", now=T2)
    rec = record(tmp_path)
    assert len(sh.ran(" push ")) == 1 and sh.ran("gh pr create") == []
    assert (rec["infra_pr"]["number"], rec["infra_pr"]["url"]) == (7, PR_URL)
    assert (rec["infra_pr"]["head"], rec["infra_pr"]["builder"]) == ("head3", B.strip())
    assert rec["infra_pr"]["pushed_at"] == T2.isoformat()
    assert rec["confirmed_revision"] == first["revision"] == rec["infra_pr"]["revision"]
    s = step(rec, "config-merge")
    assert s["state"] == "waiting" and "no ci run" in s["detail"]
    # the CI window is fresh: still waiting 29 minutes on, blocked after 30
    pr_run(tmp_path, box, B, base="base2", owned="head3", new="head4",
           now=T2 + timedelta(minutes=29))
    assert step(record(tmp_path), "config-merge")["state"] == "waiting"
    pr_run(tmp_path, box, B, base="base2", owned="head3", new="head4",
           now=T2 + timedelta(minutes=31))
    assert step(record(tmp_path), "config-merge")["state"] == "blocked"


def test_a_stable_builder_converges_without_pushing(tmp_path, box, infra_token):
    built_by(tmp_path, box, A)
    pr_run(tmp_path, box, B, owned="head1", new="head2", now=T1)
    assert pr(tmp_path)["pushed_at"] == T1.isoformat()
    for minutes in (1, 2):
        sh = pr_run(tmp_path, box, B, owned="head2", new="head3",
                    now=T1 + timedelta(minutes=minutes))
        assert sh.ran(" push ") == [] and sh.ran("gh pr create") == []
        assert (pr(tmp_path)["pushed_at"], pr(tmp_path)["head"]) == (T1.isoformat(), "head2")
    assert pr(tmp_path).get("builder") == B.strip()


def test_a_moved_infra_base_still_rebuilds_with_the_builder_recorded(
        tmp_path, box, infra_token):
    built_by(tmp_path, box, B)
    sh = pr_run(tmp_path, box, B, base="base3", now=T1)
    assert len(sh.ran(" push ")) == 1
    assert (pr(tmp_path)["base"], pr(tmp_path).get("builder")) == ("base3", B.strip())


def test_a_changed_confirmed_proposal_still_rebuilds_with_the_builder_recorded(
        tmp_path, box, infra_token):
    first = built_by(tmp_path, box, B)
    add(tmp_path, commands=[{"gate_cmd": "make check"}], epoch_ms=6000)
    run(box, FakeSh())
    rec = record(tmp_path)
    intent(tmp_path, "confirm", {"revision": rec["revision"]}, epoch_ms=7000)
    sh = pr_run(tmp_path, box, B, now=T1)
    assert len(sh.ran(" push ")) == 1 and sh.ran("gh pr create") == []
    assert pr(tmp_path)["revision"] == rec["revision"] != first["revision"]
    assert pr(tmp_path).get("builder") == B.strip()


def test_a_legacy_record_without_a_builder_is_refreshed_once(tmp_path, box, infra_token):
    rec = built_by(tmp_path, box, A)
    rec["infra_pr"].pop("builder", None)
    onboard.write_json_atomic(state(tmp_path) / "onboarding" / "analytics.json", rec)
    sh = pr_run(tmp_path, box, B, owned="head1", new="head2", now=T1)
    assert len(sh.ran(" push ")) == 1 and sh.ran("gh pr create") == []
    assert (pr(tmp_path)["number"], pr(tmp_path).get("builder")) == (7, B.strip())
    sh = pr_run(tmp_path, box, B, owned="head2", new="head3", now=T1 + timedelta(minutes=1))
    assert sh.ran(" push ") == [] and pr(tmp_path)["head"] == "head2"


@pytest.mark.parametrize("unknown", ["", "\n", failure("fatal: not a git repository")])
def test_an_unresolvable_builder_fails_retryably_without_pushing(
        tmp_path, box, infra_token, unknown):
    built_by(tmp_path, box, A)
    before = pr(tmp_path)
    sh = pr_run(tmp_path, box, unknown, now=T1)
    rec = record(tmp_path)
    assert sh.ran(" push ") == [] and rec["infra_pr"] == before
    assert "failed" in {step(rec, n)["state"] for n in ("config-pr", "config-merge")}
    pr_run(tmp_path, box, B, now=T1 + timedelta(minutes=1))
    assert (pr(tmp_path)["head"], pr(tmp_path).get("builder")) == ("head2", B.strip())


def test_a_rebuild_blocks_on_an_unrelated_remote_head(tmp_path, box, infra_token):
    built_by(tmp_path, box, A)
    before = pr(tmp_path)
    sh = pr_run(tmp_path, box, B, owned="theirs2", now=T1)
    s = step(record(tmp_path), "config-merge")
    assert s["state"] == "blocked" and "theirs2" in s["detail"] and s["link"] == PR_URL
    assert sh.ran(" push ") == [] and pr(tmp_path) == before


def test_a_failed_push_keeps_the_old_provenance_and_a_later_run_retries(
        tmp_path, box, infra_token):
    built_by(tmp_path, box, A)
    before = pr(tmp_path)
    pr_run(tmp_path, box, B, now=T1, push=failure("remote: denied"))
    assert pr(tmp_path).get("builder") == A.strip()
    assert (pr(tmp_path)["head"], pr(tmp_path)["pushed_at"]) == (
        before["head"], before["pushed_at"])
    sh = pr_run(tmp_path, box, B, now=T1 + timedelta(minutes=1))
    assert len(sh.ran(" push ")) == 1
    assert (pr(tmp_path)["head"], pr(tmp_path).get("builder")) == ("head2", B.strip())


@pytest.mark.parametrize("view,detail", [
    (pr_view("MERGED"), None), (json.dumps({"state": "CLOSED"}), "closed")])
def test_a_terminal_pr_is_not_rebuilt_for_a_new_builder(
        tmp_path, box, infra_token, view, detail):
    built_by(tmp_path, box, A)
    sh = pr_run(tmp_path, box, B, now=T1, **{"gh pr view 7": view})
    rec = record(tmp_path)
    assert sh.ran(" push ") == [] and sh.ran("gh pr create") == []
    assert rec["infra_pr"]["number"] == 7 and rec["infra_pr"].get("builder") == A.strip()
    s = step(rec, "config-merge")
    if detail:
        assert s["state"] == "blocked" and detail in s["detail"]
    else:
        assert s["state"] == "verified" and rec["merge_sha"] == "merge1"


def test_the_builder_is_the_executing_checkout_whatever_the_cwd(
        tmp_path, box, infra_token, monkeypatch):
    other = tmp_path / "other-repo"
    other.mkdir()
    monkeypatch.chdir(other)
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    sh = FakeSh(infra_routes(**NO_CI_RUN))
    sh.builder = B
    # a revision read from the cwd repo, or from the infra clone, is not the builder
    sh.route("rev-parse HEAD", lambda argv, cwd, env: (
        "cwd-rev\n" if cwd and os.path.samefile(cwd, other) else "head1\n"))
    run(box, sh)
    assert pr(tmp_path).get("builder") == B.strip()


def test_recovery_never_labels_an_adopted_commit_as_built_by_this_revision(
        tmp_path, box, infra_token):
    """mine1 was pushed before a crash lost the record; its parent and tree match."""
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    sh = FakeSh(infra_routes(open_prs=open_pr("mine1"), **NO_CI_RUN))
    sh.builder = B
    sh.route("for-each-ref", "mine1\n")
    sh.route("show -s --format=%P %T mine1", "base1 tree1\n")
    run(box, sh)
    adopted = pr(tmp_path)
    assert not (adopted["head"] == "mine1" and adopted.get("builder") == B.strip())
    assert sh.ran("gh pr create") == []
    pr_run(tmp_path, box, B, owned=adopted["head"], new="head3", now=T1,
           **{"show -s --format=%P %T mine1": "base1 tree1\n"})
    final = pr(tmp_path)
    assert final.get("builder") == B.strip() and final["head"] != "mine1"
    assert final["number"] == 7


@pytest.mark.parametrize("unknown", ["", "\n", failure("fatal: not a git repository")])
def test_an_unresolvable_builder_fails_the_initial_build_before_any_push(
        tmp_path, box, infra_token, unknown):
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    sh = FakeSh(infra_routes(**NO_CI_RUN))
    sh.builder = unknown
    run(box, sh)
    after = record(tmp_path)
    assert sh.ran(" push ") == [] and sh.ran("gh pr create") == []
    assert after.get("infra_pr") is None
    assert "failed" in {step(after, n)["state"] for n in ("config-pr", "config-merge")}
    sh = FakeSh(infra_routes(**NO_CI_RUN))
    sh.builder = B
    run(box, sh, now=T1)
    assert len(sh.ran(" push ")) == 1
    assert (pr(tmp_path)["number"], pr(tmp_path).get("builder")) == (7, B.strip())


@pytest.mark.parametrize("unknown", ["", "\n", failure("fatal: not a git repository")])
def test_an_unresolvable_builder_fails_recovery_without_adopting_the_commit(
        tmp_path, box, infra_token, unknown):
    """mine1 was pushed before a crash lost the record; the builder lookup fails."""
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    sh = FakeSh(infra_routes(open_prs=open_pr("mine1"), **NO_CI_RUN))
    sh.builder = unknown
    sh.route("for-each-ref", "mine1\n")
    sh.route("show -s --format=%P %T mine1", "base1 tree1\n")
    run(box, sh)
    after = record(tmp_path)
    assert sh.ran(" push ") == [] and sh.ran("gh pr create") == []
    assert after.get("infra_pr") is None and after["merge_sha"] == ""
    assert "failed" in {step(after, n)["state"] for n in ("config-pr", "config-merge")}
    assert step(after, "config-pr")["state"] != "verified"
    # once the builder is known, the adopted commit converges to a rebuilt PR
    sh = FakeSh(infra_routes(open_prs=open_pr("mine1"), **NO_CI_RUN))
    sh.builder = B
    sh.route("for-each-ref", "mine1\n")
    sh.route("show -s --format=%P %T mine1", "base1 tree1\n")
    run(box, sh, now=T1)
    pr_run(tmp_path, box, B, owned=pr(tmp_path)["head"], new="head3",
           now=T1 + timedelta(minutes=1),
           **{"show -s --format=%P %T mine1": "base1 tree1\n"})
    final = pr(tmp_path)
    assert final.get("builder") == B.strip() and final["head"] != "mine1"
    assert final["number"] == 7
