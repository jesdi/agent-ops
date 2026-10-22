"""Regression coverage for durable CONTROL recovery and reconfirmation."""
from copy import deepcopy

import pytest

from project_control import ConflictingConfirmation
from test_project_control_pr_deployment_acceptance import BILLING, PLAN, VERIFIED_BOARD, world


def pending(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.check_state = {"state": "pending", "failing": []}
    control.advance(op)
    return control, op, control.status(op)["pr"]["number"]


def lose_merge(world, *, merged, merge_sha=None):
    control, op, number = pending(world)
    gh = world.github
    original = gh.merge

    def merge(number, head):
        if merged:
            original(number, head)
        gh.prs[number]["merged"] = merged
        if merge_sha:
            gh.prs[number]["merge_sha"] = merge_sha
        raise ConnectionError("lost merge response")

    gh.merge = merge
    gh.check_state = {"state": "passing", "failing": []}
    with pytest.raises(ConnectionError):
        control.advance(op)
    return control, op, number


def test_unknown_merge_revision_never_uses_current_main(world):
    control, op, _ = lose_merge(world, merged=True)
    world.github.sha = "unrelated-new-main"
    world.deployer.applied = lambda: {"revision": "m100"}
    control.advance(op)
    assert control.status(op)["phase"] == "blocked"
    assert "merge revision" in control.status(op)["blocker"]
    assert control._load(op)["merge_sha"] is None


def test_known_merge_revision_completes_after_main_moves(world):
    control, op, _ = lose_merge(world, merged=True, merge_sha="m100")
    world.github.sha = "unrelated-new-main"
    world.deployer.applied = lambda: {"revision": "m100"}
    world.control().advance(op)
    assert control.status(op)["phase"] == "complete"


def test_equal_configuration_does_not_establish_merge(world):
    control, op, number = lose_merge(world, merged=False)
    world.github.advance_main({"analytics": deepcopy(world.github.prs[number]["config"]["projects"]["analytics"])})
    world.github.check_state = {"state": "failing", "failing": ["security"]}
    world.deployer.applied = lambda: {"revision": world.github.sha}
    control.advance(op)
    assert control.status(op)["phase"] == "blocked"
    assert control._load(op)["merge_sha"] is None
    assert world.github.merges == []


def test_merge_recovery_requires_the_recorded_head(world):
    control, op, number = lose_merge(world, merged=True, merge_sha="m100")
    world.github.prs[number]["head_sha"] = "unconfirmed-head"
    control.advance(op)
    assert control.status(op)["phase"] == "blocked"
    assert control._load(op)["merge_sha"] is None


def install_refresh(world):
    calls = []

    def update_pr(number, head_sha, base_sha, config):
        pr = world.github.prs[number]
        assert pr["head_sha"] == head_sha
        pr.update(base_sha=base_sha, config=deepcopy(config), head_sha=head_sha + "r")
        world.github.check_state = {"state": "pending", "failing": []}
        calls.append(number)
        return deepcopy(pr)

    world.github.update_pr = update_pr
    return calls


def test_unrelated_main_change_refreshes_existing_pr_and_waits(world):
    control, op, number = pending(world)
    calls = install_refresh(world)
    world.github.advance_main({"billing": dict(BILLING, models="new-model")})
    world.github.check_state = {"state": "passing", "failing": []}
    control.advance(op)
    assert calls == [number]
    assert control.status(op)["phase"] == "published"
    assert world.github.merges == []
    world.github.check_state = {"state": "passing", "failing": []}
    world.control().advance(op)
    assert control.status(op)["phase"] == "complete"
    assert len(world.github.created) == 1
    assert world.github.config["projects"]["billing"]["models"] == "new-model"


def test_explicit_reconfirmation_refreshes_expected_project_and_delta(world):
    control, op, number = pending(world)
    install_refresh(world)
    world.github.advance_main({"analytics": dict(BILLING)})
    control.advance(op)
    assert "confirm again" in control.status(op)["blocker"]
    revised = dict(PLAN, revision="r2", models="new-model")
    with pytest.raises(ConflictingConfirmation):
        control.confirm(revised)
    assert control.confirm(revised, supersede=True) == op
    assert world.control().confirm(revised, supersede=True) == op
    control.advance(op)
    assert world.github.prs[number]["config"]["projects"]["analytics"]["models"] == "new-model"
    world.github.check_state = {"state": "passing", "failing": []}
    world.control().advance(op)
    assert control.status(op)["phase"] == "complete"
    assert len(world.github.created) == 1


def test_same_plan_can_be_explicitly_reconfirmed(world):
    control, op, _ = pending(world)
    install_refresh(world)
    world.github.advance_main({"analytics": dict(BILLING)})
    control.advance(op)
    assert control.confirm(PLAN, supersede=True) == op
    control.advance(op)
    assert control.status(op)["phase"] == "published"


def test_repository_aliases_share_identity_and_ownership(world):
    control = world.control()
    alias = deepcopy(PLAN)
    alias["repositories"][0]["repo"] = "https://github.com/example/analytics.git"
    assert control.confirm(alias) == control.confirm(PLAN)
    world.github.advance_main({"other": {"repositories": ["https://github.com/example/analytics.git"]}})
    op = control.confirm(PLAN)
    control.advance(op)
    assert "already owned" in control.status(op)["blocker"]
    assert world.github.created == []


def test_lost_refresh_response_reuses_pr_after_restart(world):
    control, op, number = pending(world)
    calls = install_refresh(world)
    refresh = world.github.update_pr

    def lost(*args):
        refresh(*args)
        raise ConnectionError("lost refresh response")

    world.github.update_pr = lost
    world.github.advance_main({"billing": dict(BILLING, models="new-model")})
    with pytest.raises(ConnectionError):
        control.advance(op)
    restarted = world.control()
    restarted.advance(op)
    assert calls == [number]
    assert restarted.status(op)["phase"] == "published"
    world.github.check_state = {"state": "passing", "failing": []}
    restarted.advance(op)
    assert restarted.status(op)["phase"] == "complete"
    assert len(world.github.created) == 1


def test_lost_create_then_main_change_refreshes_the_same_pr(world):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.lose_create_response = True
    with pytest.raises(ConnectionError):
        control.advance(op)
    calls = install_refresh(world)
    world.github.advance_main({"billing": dict(BILLING, models="new-model")})
    world.control().advance(op)
    assert calls == [100]
    assert len(world.github.created) == 1


def test_refresh_requires_new_approval(world):
    control, op, _ = pending(world)
    install_refresh(world)
    world.github.advance_main({"billing": dict(BILLING, models="new-model")})
    control.advance(op)
    world.github.check_state = {"state": "passing", "failing": []}
    world.github.approval_state = {"required": True, "granted": False}
    control.advance(op)
    assert "approval" in control.status(op)["blocker"]
    assert world.github.merges == []


def test_reconfirmation_cannot_replace_an_uncertain_merge(world):
    control, op, _ = lose_merge(world, merged=True, merge_sha="m100")
    revised = dict(PLAN, revision="r2", models="new-model")
    with pytest.raises(ConflictingConfirmation):
        control.confirm(revised, supersede=True)
    world.control().advance(op)
    assert control.status(op)["phase"] == "complete"
    assert world.github.config["projects"]["analytics"]["models"] == PLAN["models"]


def test_tampered_stale_pr_cannot_be_refreshed(world):
    control, op, number = pending(world)
    calls = install_refresh(world)
    world.github.push_to_pr(number, {"intruder": dict(BILLING)})
    world.github.advance_main({"billing": dict(BILLING, models="new-model")})
    control.advance(op)
    assert control.status(op)["phase"] == "blocked"
    assert calls == []
    assert world.github.merges == []


def test_publication_preserves_whole_configuration_through_refresh_and_deploy(world):
    gh = world.github
    gh.main = lambda: (gh.sha, deepcopy(gh.config))
    globals_before = dict(defaults={"model": "inherit"}, schema_version=3,
                          capacity=10, state_dir="/srv/state",
                          targets=[{"repo": "example/legacy", "enabled": True}])
    gh.config.update(deepcopy(globals_before))
    control, op, number = pending(world)
    assert gh.prs[number]["config"] == {**globals_before, "projects": {
        **gh.config["projects"], "analytics": {
            "repositories": PLAN["repositories"], "board": VERIFIED_BOARD,
            "models": PLAN["models"], "desired_enabled": False}}}
    install_refresh(world)
    gh.config["capacity"] = 8
    gh.sha = "s2"
    control.advance(op)
    gh.check_state = {"state": "passing", "failing": []}
    world.control().advance(op)
    assert control.status(op)["phase"] == "complete"
    assert world.deployer.live_config == gh.config
    assert {key: gh.config[key] for key in globals_before} == {**globals_before, "capacity": 8}


def expanded_plan():
    plan = deepcopy(PLAN)
    plan["revision"] = "r2"
    plan["repositories"].append(dict(plan["repositories"][0], repo="example/analytics-api"))
    return plan


@pytest.mark.parametrize("lost_create", [False, True])
def test_membership_supersedes_one_durable_operation_and_pr(world, lost_create):
    control = world.control()
    op = control.confirm(PLAN)
    world.github.check_state = {"state": "pending", "failing": []}
    world.github.lose_create_response = lost_create
    if lost_create:
        with pytest.raises(ConnectionError):
            control.advance(op)
    else:
        control.advance(op)
    calls = install_refresh(world)
    revised = expanded_plan()
    with pytest.raises(ConflictingConfirmation):
        world.control().confirm(revised)
    assert world.control().confirm(revised, supersede=True) == op
    world.control().advance(op)
    assert calls == [100]
    assert world.github.merges == []
    world.github.check_state = {"state": "passing", "failing": []}
    control.advance(op)
    assert control.status(op)["phase"] == "complete"
    assert world.github.config["projects"]["analytics"]["repositories"] == revised["repositories"]
    assert len(world.github.created) == 1
    assert len(list((world.state / "operations").glob("*.json"))) == 1
    assert control._load(op)["confirmations"][0]["plan"] == PLAN


def test_membership_change_cannot_supersede_an_uncertain_merge(world):
    control, op, _ = lose_merge(world, merged=True, merge_sha="m100")
    with pytest.raises(ConflictingConfirmation, match="resolve the previous merge"):
        world.control().confirm(expanded_plan(), supersede=True)
    assert len(list((world.state / "operations").glob("*.json"))) == 1
    control.advance(op)
    assert control.status(op)["phase"] == "complete"


def test_membership_change_reuses_a_legacy_operation_id(world):
    import hashlib
    import json

    control, op, _ = pending(world)
    key = json.dumps([PLAN["name"], [r["repo"] for r in PLAN["repositories"]]])
    legacy_id = "op-" + hashlib.sha256(key.encode()).hexdigest()[:16]
    control._path(op).rename(control._path(legacy_id))
    assert world.control().confirm(expanded_plan(), supersede=True) == legacy_id
    calls = install_refresh(world)
    world.control().advance(legacy_id)
    assert calls == [100]
    assert len(list((world.state / "operations").glob("*.json"))) == 1


def test_membership_supersede_preserves_uncertain_refresh_history(world):
    control, op, number = pending(world)
    calls = install_refresh(world)
    update = world.github.update_pr

    def lost(*args):
        update(*args)
        raise ConnectionError("lost refresh response")

    world.github.update_pr = lost
    world.github.advance_main({"billing": dict(BILLING, models="updated")})
    with pytest.raises(ConnectionError):
        control.advance(op)
    assert world.control().confirm(expanded_plan(), supersede=True) == op
    world.github.update_pr = update
    world.control().advance(op)
    assert calls == [number, number]
    assert len(world.github.created) == 1
    world.github.check_state = {"state": "passing", "failing": []}
    world.control().advance(op)
    assert control.status(op)["phase"] == "complete"
    assert world.github.config["projects"]["analytics"]["repositories"] == expanded_plan()["repositories"]


@pytest.mark.parametrize("legacy_id", [False, True])
def test_membership_confirmation_waits_for_the_project_worker(world, legacy_id):
    import threading
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    control, op, _ = pending(world)
    if legacy_id:
        control._path(op).rename(control._path("op-legacy"))
        op = "op-legacy"
    entered, release, confirming = threading.Event(), threading.Event(), threading.Event()
    find = world.github.find_pr

    def held_find(branch):
        entered.set()
        assert release.wait(5)
        return find(branch)

    def supersede():
        confirming.set()
        return world.control().confirm(expanded_plan(), supersede=True)

    world.github.find_pr = held_find
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(control.advance, op)
        try:
            assert entered.wait(5)
            confirmation = pool.submit(supersede)
            assert confirming.wait(5)
            with pytest.raises(TimeoutError):
                confirmation.result(timeout=0.1)
        finally:
            release.set()
        worker.result(timeout=5)
        assert confirmation.result(timeout=5) == op
    calls = install_refresh(world)
    control.advance(op)
    assert calls == [100]
    assert world.github.merges == []
