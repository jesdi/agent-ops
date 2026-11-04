"""Fixer additions for ticket 02: timer-run recheck, clone credentials, empty repo list."""
import json
from pathlib import Path

from dispatcher import github_app, onboard
from tests.test_onboard import add, box, infra_token, record, state, step  # noqa: F401
from tests.test_onboard_app import JESDI, FakeApp, run, sh_for


def test_timer_run_blocks_an_unconfirmed_project_that_lost_access(tmp_path, box):
    app = FakeApp()
    add(tmp_path)
    run(box, sh_for(), app)
    assert step(record(tmp_path), "resolve")["state"] == "verified"
    app.remove("jesdi/analytics")
    run(box, sh_for(), app)
    rec = record(tmp_path)
    s = step(rec, "confirm")
    assert (s["state"], s["link"]) == ("blocked", JESDI.settings_url)
    assert "jesdi/analytics" in s["detail"]
    app.add("jesdi/analytics")
    run(box, sh_for(), app)
    assert "review the proposal" in step(record(tmp_path), "confirm")["detail"]


def test_recovering_lost_access_keeps_the_inspected_proposal(tmp_path, box):
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / ".git").mkdir(parents=True)
        (root / ".backlog").mkdir()
        (root / ".backlog/project-meta.json").write_text(
            json.dumps({"owner": "jesdi", "projectNumber": 4}))
        return ""

    def sh():
        x = sh_for()
        x.route("git clone", clone)
        return x
    app = FakeApp()
    onboard.write_intent(state(tmp_path), "add", "analytics", {
        "repos": ["jesdi/analytics"], "board": None, "commands": [{"gate_cmd": None}],
        "inspect": True}, "agent", 1000)
    run(box, sh(), app)
    rec = onboard.load_record(state(tmp_path), "analytics")
    onboard.write_intent(state(tmp_path), "skip-inspection", "analytics",
                         {"revision": rec.revision}, "agent", 1001)
    run(box, sh(), app)
    inspected = record(tmp_path)
    assert step(inspected, "inspect")["state"] == "verified"
    assert inspected["proposal"]["board"] == {"owner": "jesdi", "number": 4}
    app.remove("jesdi/analytics")
    run(box, sh(), app)
    assert step(record(tmp_path), "confirm")["link"] == JESDI.settings_url
    app.add("jesdi/analytics")
    run(box, sh(), app)
    rec = record(tmp_path)
    assert [step(rec, n)["state"] for n in ("resolve", "clone", "inspect")] == ["verified"] * 3
    assert (rec["proposal"], rec["revision"]) == (inspected["proposal"], inspected["revision"])
    assert step(rec, "confirm")["link"] == ""


def test_timer_run_retries_after_an_app_error(tmp_path, box):
    app = FakeApp()
    add(tmp_path)
    run(box, sh_for(), app)
    app.fail = github_app.AppError("500")
    run(box, sh_for(), app)
    assert step(record(tmp_path), "resolve")["state"] == "verified"


def test_clone_clears_stored_credential_helpers(tmp_path, box):
    app = FakeApp()
    add(tmp_path)
    sh = sh_for()
    run(box, sh, app)
    [(argv, _, env)] = sh.ran("git clone")
    assert env["GIT_CONFIG_VALUE_0"] == "" and "gh auth git-credential" in env["GIT_CONFIG_VALUE_1"]


def test_unparseable_repository_blocks_with_an_app(tmp_path, box):
    add(tmp_path, repos=("not a repo",))
    run(box, sh_for(), FakeApp())
    assert "not a GitHub repository" in step(record(tmp_path), "resolve")["detail"]
