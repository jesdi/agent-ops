"""Ticket 11 fixes: scope reaches provisioning, spec-repo choice, approval binding."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dispatcher import artifacts, intents, main, state
from dispatcher.state import TaskBundle, TaskRepository
from tests.webfakes import make_task

FRONT, BACK = "example/front", "example/back"


def cfg(tmp_path):
    repos = [SimpleNamespace(repo=FRONT), SimpleNamespace(repo=BACK)]
    return SimpleNamespace(state_dir=str(tmp_path), projects=[SimpleNamespace(key="shop", repositories=repos)])


def record(tmp_path, spec_repo="", back_wt="/nowhere"):
    state.save_bundle(tmp_path, "shop", 4, TaskBundle(
        (TaskRepository(FRONT, "/f", "b"), TaskRepository(BACK, back_wt, "b")), spec_repo=spec_repo),
        repo=FRONT)


def test_recorded_scope_names_the_other_project_repositories(tmp_path):
    c = cfg(tmp_path)
    target = SimpleNamespace(name="shop", repo=FRONT)
    assert main._bundle_members(c, target, 4) == ()
    record(tmp_path)
    assert [m.repo for m in main._bundle_members(c, target, 4)] == [BACK]
    assert main._project_members(c) == {"shop": frozenset({FRONT, BACK})}


def test_spec_repo_intent_records_choice_and_refuses_outsider(tmp_path):
    record(tmp_path)
    c = cfg(tmp_path)

    def intent(repo):
        return intents.Intent("spec-repo", "shop", 4, {"spec_repo": repo}, "op", "", Path("x"), FRONT)
    main._apply_spec_repo_intent(c, None, intent("example/other"))
    assert state.read_bundle(tmp_path, "shop", 4, repo=FRONT).spec_repo == ""
    main._apply_spec_repo_intent(c, None, intent(BACK))
    assert state.read_bundle(tmp_path, "shop", 4, repo=FRONT).spec_repo == BACK


def test_approval_fingerprint_follows_scope_and_secondary_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    front, back = tmp_path / "f", tmp_path / "b"
    for wt in (front, back):
        (wt / ".agent").mkdir(parents=True)
    (front / ".agent" / "review.html").write_text(artifacts.REVIEW_PAGE_MARKER)
    (back / ".agent" / "tickets").mkdir()
    (back / ".agent" / "tickets" / "01-one.md").write_text("one")
    task = make_task(4, target="shop", repo=FRONT, worktree=str(front))

    def fingerprint():
        return artifacts.task_plan_revision(task, ".agent/review.html").fingerprint
    plain = fingerprint()
    assert plain
    record(tmp_path, back_wt=str(back))
    scoped = fingerprint()
    assert scoped != plain
    record(tmp_path, spec_repo=BACK, back_wt=str(back))
    chosen = fingerprint()
    assert chosen != scoped
    (back / ".agent" / "tickets" / "01-one.md").write_text("two")
    assert fingerprint() != chosen
    state.confirm_scope(tmp_path, "shop", 4, [], repo=FRONT, members=[FRONT, BACK])
    assert fingerprint() != chosen
    (back / ".agent" / "tickets" / "01-one.md").unlink()
    (back / ".agent" / "tickets" / "01-one.md").symlink_to(front / ".agent" / "review.html")
    assert artifacts.task_plan_revision(task, ".agent/review.html").fingerprint == ""


def test_gh_wrapper_picks_the_token_of_the_repository(tmp_path):
    root = Path(__file__).resolve().parent.parent / "dispatcher" / "credential_commands" / "gh"
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho $GH_TOKEN\n")
    fake.chmod(0o755)
    wrapper = tmp_path / "wrap"
    wrapper.write_text(root.read_text().replace("/usr/bin/gh", str(fake)))
    (tmp_path / "t").write_text("front")
    (tmp_path / "t.example~back").write_text("back")
    env = {"GH_TOKEN_FILE": str(tmp_path / "t"), "PATH": "/usr/bin:/bin"}

    def run(*args, cwd=tmp_path):
        return subprocess.run(["sh", str(wrapper), *args], env=env, cwd=cwd,
                              capture_output=True, text=True).stdout.strip()
    assert run("pr") == "front"
    assert run("-R", "example/back", "pr") == "back"
    assert run("--repo=example/back", "pr") == "back"
    assert run("--repo", "example/nope", "pr") == "front"
    repo = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "git@github.com:example/back.git"], check=True)
    assert run("pr", cwd=repo) == "back"


def test_definition_scope_is_read_from_the_issue_body_and_validated(tmp_path):
    c = cfg(tmp_path)
    target = SimpleNamespace(name="shop", repo=FRONT, sandbox=True)
    body = f"Do it.\nRepositories: {BACK}\nSpec repository: {BACK}\n"
    deps = SimpleNamespace(github=SimpleNamespace(issue_view=lambda repo, n: {"body": body}))
    assert main._declared_scope(c, deps, target, 4, False) == ((FRONT, BACK), BACK)
    deps.github.issue_view = lambda repo, n: {"body": "Repositories: example/other"}
    with pytest.raises(state.ScopeRefused):
        main._declared_scope(c, deps, target, 4, False)


def test_spec_publication_waits_for_and_follows_the_selected_repository(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    task = SimpleNamespace(target="shop", issue=4, repo=FRONT, worktree="/f", branch="b", spec_path="specs/x/spec.md")
    target = SimpleNamespace(repo=FRONT)
    seen = []
    monkeypatch.setattr(main.spec_publish, "ensure_published", lambda **kw: seen.append(kw))
    record(tmp_path)
    assert main._publish_spec(c, task, target, False) is None and not seen
    state.select_spec_repo(tmp_path, "shop", 4, BACK, repo=FRONT, members=[FRONT, BACK])
    main._publish_spec(c, task, target, False)
    assert (seen[0]["worktree"], seen[0]["repo"]) == ("/nowhere", BACK)
