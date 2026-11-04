"""Fixer additions for ticket 02: timer-run recheck, clone credentials, empty repo list."""
from dispatcher import github_app
from tests.test_onboard import add, box, infra_token, record, step  # noqa: F401
from tests.test_onboard_app import FakeApp, run, sh_for


def test_timer_run_blocks_an_unconfirmed_project_that_lost_access(tmp_path, box):
    app = FakeApp()
    add(tmp_path)
    run(box, sh_for(), app)
    assert step(record(tmp_path), "resolve")["state"] == "verified"
    app.remove("jesdi/analytics")
    run(box, sh_for(), app)
    assert step(record(tmp_path), "resolve")["state"] == "blocked"


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
