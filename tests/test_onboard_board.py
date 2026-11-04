"""The board step (increment 7, #230): the confirmed board gets the fixed
structure, every repository the label set, and every open issue one card,
however often onboarding runs. GitHub is the stateful FakeGitHub."""
import json
from pathlib import Path

import pytest
import yaml

from dispatcher import onboard
from tests.fake_github import FakeGitHub, STATUSES, complete
from tests.test_onboard import (
    NO_CI_RUN, FakeSh, add, box, gh_repo, infra_routes, infra_token, intent, record,
    run, scratch, state, step)

TAXONOMY = {"inbox", "bug", "enhancement", "documentation", "question", "frontend",
            "backend", "infra", "ci", "security", "performance", "testing",
            "dependencies", "auto", "human-required", "spec-ready"}


def github(**fields) -> tuple[FakeGitHub, dict]:
    gh = FakeGitHub()
    b = gh.board(fields={**complete(), **fields} if fields else None)
    return gh, b


def origin(argv, cwd, env):
    """The clone at <root>/<project>/<name> is of jesdi/<name>."""
    return f"https://github.com/jesdi/{Path(argv[2]).name.removesuffix('.partial')}.git\n"


def sh_for(gh: FakeGitHub, **routes) -> FakeSh:
    return FakeSh({"gh api graphql": gh, "gh label": gh, "remote get-url origin": origin,
                   **routes})


def to_confirm(tmp_path, box, gh, routes=None, **kw) -> dict:
    add(tmp_path, **kw)
    run(box, sh_for(gh, **(routes or {})))
    return record(tmp_path)


def through_board(tmp_path, box, gh, routes=None, **kw) -> dict:
    """Added, confirmed, then one run as far as config-pr."""
    rec = to_confirm(tmp_path, box, gh, routes, **kw)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN), **(routes or {})))
    return record(tmp_path)


def blocker(rec) -> dict:
    """The first step that isn't verified: board checks run at resolve
    without inspection, and at confirm."""
    return next(s for s in rec["steps"] if s["state"] != "verified")


def test_board_step_adds_what_is_missing_and_imports_open_issues_once(
        tmp_path, box, infra_token):
    gh, b = github(Effort=None, Boost=None, Area=None,
                   Status=("SINGLE_SELECT", ("Inbox", "Ready", "Done")))
    for name in ("Effort", "Boost", "Area"):
        del b["fields"][name]
    ready_id = gh.option_id(b, "Status", "Ready")
    triaged, fresh = gh.issue("jesdi/analytics", 1), gh.issue("jesdi/analytics", 2)
    closed = gh.issue("jesdi/analytics", 3, state="CLOSED")
    pr = gh.issue("jesdi/analytics", 4, kind="PULL_REQUEST")
    gh.card(b, triaged, "Ready", Impact=3)
    gh.labels["jesdi/analytics"] = {"bug": ("000000", "ours")}

    rec = through_board(tmp_path, box, gh)

    assert step(rec, "board")["state"] == "verified"
    assert step(rec, "config-pr")["state"] == "verified"
    types = {n: f["type"] for n, f in b["fields"].items()}
    assert {n: types[n] for n in ("Effort", "Boost")} == {"Effort": "NUMBER", "Boost": "NUMBER"}
    assert [o["name"] for o in b["fields"]["Area"]["options"]] == [
        "feature", "bug", "infra", "docs", "research"]
    assert [o["name"] for o in b["fields"]["Status"]["options"]] == [
        "Inbox", "Ready", "Done", "In progress", "Wont do"]
    assert gh.option_id(b, "Status", "Ready") == ready_id
    assert gh.status(b, triaged) == "Ready" and gh.item_of(b, triaged)["values"]["Impact"] == 3
    assert gh.status(b, fresh) == "Inbox"
    assert gh.item_of(b, closed) is None and gh.item_of(b, pr) is None
    labels = gh.labels["jesdi/analytics"]
    assert set(labels) == TAXONOMY | {"track:standard"}
    assert labels["bug"] == ("000000", "ours")
    assert {"kind": "board", "ref": "https://github.com/users/jesdi/projects/4",
            "state": "in use"} in rec["resources"]


def test_a_rerun_of_the_board_step_never_duplicates_or_resets_triage(
        tmp_path, box, infra_token):
    gh, b = github()
    fresh = gh.issue("jesdi/analytics", 2)
    through_board(tmp_path, box, gh)
    b["items"][0]["values"]["Status"] = gh.option_id(b, "Status", "Ready")
    later = gh.issue("jesdi/analytics", 5)
    add(tmp_path, commands=[{"gate_cmd": "make check"}], epoch_ms=6000)
    run(box, sh_for(gh))
    intent(tmp_path, "confirm", {"revision": record(tmp_path)["revision"]}, epoch_ms=7000)
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    assert step(record(tmp_path), "board")["state"] == "verified"
    assert len(b["items"]) == 2
    assert gh.status(b, fresh) == "Ready" and gh.status(b, later) == "Inbox"


def test_same_issue_number_in_two_repositories_gets_two_cards(tmp_path, box, infra_token):
    gh, b = github()
    one, two = gh.issue("jesdi/analytics", 12), gh.issue("jesdi/web", 12)
    rec = through_board(tmp_path, box, gh, repos=["jesdi/analytics", "jesdi/web"],
                        routes={"gh api repos/jesdi/web": gh_repo(303, "jesdi/web")})
    assert step(rec, "board")["state"] == "verified"
    assert gh.status(b, one) == gh.status(b, two) == "Inbox"
    assert set(gh.labels) == {"jesdi/analytics", "jesdi/web"}


def test_the_board_is_linked_to_repositories_of_its_owner_only(tmp_path, box, infra_token):
    gh, b = github()
    rec = through_board(tmp_path, box, gh, repos=["jesdi/analytics", "acme/web"],
                        routes={"gh api repos/acme/web": gh_repo(303, "acme/web"),
                                "remote get-url origin": lambda argv, cwd, env: origin(
                                    argv, cwd, env).replace("jesdi/web", "acme/web")})
    assert step(rec, "board")["state"] == "verified"
    assert b["repos"] == {"R_202"}
    assert "acme/web" in step(rec, "board")["detail"]


def test_planned_additions_are_listed_for_confirm(tmp_path, box):
    gh, b = github()
    del b["fields"]["Boost"]
    rec = to_confirm(tmp_path, box, gh)
    assert step(rec, "confirm")["state"] == "blocked"
    assert "add number field Boost" in rec["board_review"]
    assert "add labels on jesdi/analytics: " + ", ".join(sorted(TAXONOMY | {"track:standard"})) \
        in rec["board_review"]


def test_a_field_of_the_wrong_type_blocks_confirm_and_the_board_step(tmp_path, box, infra_token):
    gh, b = github(Impact="TEXT")
    rec = to_confirm(tmp_path, box, gh)
    s = blocker(rec)
    assert s["state"] == "blocked" and "Impact" in s["detail"] and "number" in s["detail"]
    # a clash that appears after confirm blocks the board step, not confirm
    gh, b = github()
    gh.issue("jesdi/analytics", 1)
    rec = to_confirm(tmp_path, box, gh, epoch_ms=2000)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    gh.fail_after = "addProjectV2ItemById"      # the board step fails once, after confirm
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    assert step(record(tmp_path), "board")["state"] == "failed"
    b["fields"]["Score"]["type"] = "TEXT"
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    rec = record(tmp_path)
    assert step(rec, "confirm")["state"] == "verified"
    assert step(rec, "board")["state"] == "blocked" and "Score" in step(rec, "board")["detail"]
    assert step(rec, "board")["link"] == "https://github.com/users/jesdi/projects/4"


def test_a_card_from_another_repository_blocks_and_is_left_alone(tmp_path, box):
    gh, b = github()
    gh.card(b, gh.issue("jesdi/billing", 9, state="CLOSED"), "Done")
    gh.card(b, None)                                   # a draft is fine
    rec = to_confirm(tmp_path, box, gh)
    s = blocker(rec)
    assert s["state"] == "blocked"
    assert "https://github.com/jesdi/billing/issues/9" in s["detail"]
    assert len(b["items"]) == 2


def test_an_unreadable_card_is_noted_not_refused(tmp_path, box):
    gh, b = github()
    gh.card(b, gh.issue("jesdi/secret", 1, title="hidden"), redacted=True)
    gh.card(b, gh.issue("jesdi/analytics", 1, title="shown"), redacted=True)
    rec = to_confirm(tmp_path, box, gh)
    assert step(rec, "confirm")["detail"].startswith("review the proposal")
    assert "1 card the board token can't read" in " ".join(rec["board_review"])
    assert onboard.can_confirm(onboard.load_record(state(tmp_path), "analytics"))


def test_a_new_board_is_created_once_even_after_a_crash(tmp_path, box, infra_token):
    gh = FakeGitHub()
    issue = gh.issue("jesdi/analytics", 1)
    rec = to_confirm(tmp_path, box, gh, board={"new": True})
    assert rec["proposal"]["board"] == {"owner": "jesdi", "new": True, "title": "analytics Backlog"}
    assert "create board 'analytics Backlog' for jesdi" in rec["board_review"]
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    gh.fail_after = "createProjectV2("
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    assert step(record(tmp_path), "board")["state"] == "failed"
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    rec = record(tmp_path)
    assert step(rec, "board")["state"] == "verified"
    assert [b["title"] for b in gh.boards.values()] == ["analytics Backlog"]
    b = gh.boards[("jesdi", 1)]
    assert [o["name"] for o in b["fields"]["Status"]["options"]] == list(STATUSES)
    assert gh.status(b, issue) == "Inbox"
    assert rec["board_number"] == 1
    pushed = yaml.safe_load((scratch(tmp_path) / "config/targets.yaml").read_text())
    entry = next(p for p in pushed["projects"] if p["name"] == "analytics")
    assert entry["board"] == {"owner": "jesdi", "number": 1}


def test_an_open_record_from_before_gains_the_board_step_before_config_pr(tmp_path, box):
    gh, _b = github()
    to_confirm(tmp_path, box, gh)
    p = state(tmp_path) / "onboarding" / "analytics.json"
    old = json.loads(p.read_text())
    old["steps"] = [s for s in old["steps"] if s["name"] != "board"]
    old.pop("board_number"), old.pop("board_review")
    p.write_text(json.dumps(old))
    rec = onboard.load_record(state(tmp_path), "analytics")
    assert [s.name for s in rec.steps] == list(onboard.STEPS)
    assert rec.step("board").state is onboard.StepState.PENDING


def test_a_complete_record_from_before_stays_complete(tmp_path, box):
    gh, _b = github()
    to_confirm(tmp_path, box, gh)
    p = state(tmp_path) / "onboarding" / "analytics.json"
    old = json.loads(p.read_text())
    old["status"] = "complete"
    old["steps"] = [{**s, "state": "verified"} for s in old["steps"] if s["name"] != "board"]
    p.write_text(json.dumps(old))
    rec = onboard.load_record(state(tmp_path), "analytics")
    assert rec.step("board").state is onboard.StepState.VERIFIED


def test_two_boards_with_the_new_boards_title_block_instead_of_guessing(
        tmp_path, box, infra_token):
    gh = FakeGitHub()
    rec = to_confirm(tmp_path, box, gh, board={"new": True})
    gh.board("jesdi", 7, title="analytics Backlog")
    gh.board("jesdi", 8, title="analytics Backlog")
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    s = step(record(tmp_path), "board")
    assert s["state"] == "blocked" and "2 boards titled 'analytics Backlog'" in s["detail"]
    assert len(gh.boards) == 2


def test_a_foreign_card_that_appears_after_confirm_blocks_the_board_step(
        tmp_path, box, infra_token):
    gh, b = github()
    gh.issue("jesdi/analytics", 1)
    rec = to_confirm(tmp_path, box, gh)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    gh.fail_after = "addProjectV2ItemById"      # the board step fails once, after confirm
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    gh.card(b, gh.issue("jesdi/billing", 9))
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    rec = record(tmp_path)
    assert step(rec, "confirm")["state"] == "verified"
    s = step(rec, "board")
    assert s["state"] == "blocked" and "https://github.com/jesdi/billing/issues/9" in s["detail"]
    assert s["link"] == "https://github.com/users/jesdi/projects/4"


def test_a_board_made_before_a_lost_save_is_found_by_title_and_reused(
        tmp_path, box, infra_token):
    """The create ran but the record never kept its number: the rerun finds
    the board by its title, adds only what is missing and keeps every value."""
    gh = FakeGitHub()
    issue = gh.issue("jesdi/analytics", 1)
    rec = to_confirm(tmp_path, box, gh, board={"new": True})
    made = gh.board("jesdi", 3, title="analytics Backlog", fields={"Status": ("SINGLE_SELECT", STATUSES)})
    gh.card(made, issue, "Ready")
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    run(box, sh_for(gh, **infra_routes(**NO_CI_RUN)))
    rec = record(tmp_path)
    assert step(rec, "board")["state"] == "verified" and rec["board_number"] == 3
    assert list(gh.boards) == [("jesdi", 3)]
    assert gh.status(made, issue) == "Ready" and "Impact" in made["fields"]


def test_a_status_spelled_in_another_case_blocks_instead_of_adding_a_twin(tmp_path, box):
    gh, b = github(Status=("SINGLE_SELECT", ("Todo", "In Progress", "Done")))
    s = blocker(to_confirm(tmp_path, box, gh))
    assert s["state"] == "blocked"
    assert "Status option 'In progress' is spelled 'In Progress' here" in s["detail"]
    assert [o["name"] for o in b["fields"]["Status"]["options"]] == ["Todo", "In Progress", "Done"]


def test_a_card_left_without_a_status_gets_inbox(tmp_path, box, infra_token):
    gh, b = github()
    bare = gh.issue("jesdi/analytics", 1)
    gh.card(b, bare)
    through_board(tmp_path, box, gh)
    assert gh.status(b, bare) == "Inbox" and len(b["items"]) == 1


def test_more_than_ten_foreign_cards_are_listed_ten_and_counted(tmp_path, box):
    gh, b = github()
    for n in range(1, 13):
        gh.card(b, gh.issue("jesdi/billing", n))
    s = blocker(to_confirm(tmp_path, box, gh))
    assert "https://github.com/jesdi/billing/issues/10 and 2 more" in s["detail"]
    assert "issues/11" not in s["detail"]


def test_existing_area_options_and_extra_fields_are_left_alone(tmp_path, box, infra_token):
    gh, b = github(Area=("SINGLE_SELECT", ("api", "web")), Sprint="ITERATION",
                   Status=("SINGLE_SELECT", (*STATUSES, "Blocked")))
    area = [dict(o) for o in b["fields"]["Area"]["options"]]
    rec = through_board(tmp_path, box, gh)
    assert step(rec, "board")["state"] == "verified"
    assert b["fields"]["Area"]["options"] == area
    assert b["fields"]["Sprint"]["type"] == "ITERATION"
    assert [o["name"] for o in b["fields"]["Status"]["options"]] == [*STATUSES, "Blocked"]
