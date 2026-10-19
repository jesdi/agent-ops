"""Acceptance tests for ticket 11 (project-onboarding): one task across its
required repositories.

Spec: specs/project-onboarding/spec.md S54 (R7, R18, R19, R22, R23); design.md
Q1 (scope source) and Q2 (spec/plan repository). Seam: SESSION --
Sessions.spawn_stage/resume/end and waitd.handle_ping, over the fake rootless
box runtime (tests/test_task_sandbox_acceptance.py), a fake GitHub App edge
(tests/test_execution_credentials_acceptance.py) and a fake herdr.

Interface these tests define:
- `state.TaskBundle(repositories=(TaskRepository(repo, worktree, branch), ...),
  scope_revision, source, spec_repo)` is the controller-recorded scope of one
  task. `state.save_bundle/read_bundle(state_dir, target, issue, repo=ORIGIN)`
  persist it; `state.select_spec_repo(..., members=)` records the operator's
  spec/plan repository and `state.confirm_scope(..., add=, members=)` records a
  widened scope as a new revision; both raise `state.ScopeRefused` outside
  `members`.
- `Sessions(members={project: member repos}, sandbox=TaskSandbox)`;
  `Sessions.spawn_stage(..., repo=ORIGIN, bundle=TaskBundle)` validates the
  bundle against `members[target]` BEFORE any credential, tab or sandbox call
  (`ScopeRefused`), records it, registers the execution with
  `credentials.register_execution(name, (repo, ...), "task")` and asks for one
  token per repository, each limited to that repository. `resume` takes no
  bundle: it reads the recorded one. Without a bundle the launch is the
  single-repository launch of its `repo`.
- `SandboxRequest.members=(SandboxMember(repo, clone, services, setup, gate,
  branch),)` adds repositories to the SAME `ServicePlan`; `plan.checkouts` maps
  repo -> checkout, `Executed.repo` names the repository a command ran in and
  `engine.exec(name, command, workdir=<mount dest of that checkout>)` carries it.
- `task_artifacts.collect` reads the bundle: a manifest entry may name its
  `repo`; stored items carry `repo` and `task_revision`; with no `spec_repo`
  selected nothing is published and `index.awaiting_spec_repo` is True.
"""
import json
import subprocess as sp
from pathlib import Path
from types import SimpleNamespace

import pytest

from dispatcher import artifacts, containers, herdr, intents, main, sandbox_runtime, state, task_artifacts
from dispatcher.execution_credentials import (
    CredentialDenied, ExecutionCredentials, ExecutionScope, Installation)
from dispatcher.sandbox import Limits, SandboxMember, SandboxRequest, TaskSandbox
from dispatcher.sandbox_runtime import execution_ref
from dispatcher.sessions import Sessions, session_name
from dispatcher.state import ScopeRefused, TaskBundle, TaskRepository
from dispatcher.waitd import handle_ping
from tests.test_execution_credentials_acceptance import App, Clock
from tests.test_execution_identity_acceptance import Herdr, podman  # noqa: F401
from tests.test_task_sandbox_acceptance import GIB, Box, compose, git, make_clone
from tests.webfakes import make_task
from web.sources import Sources

T, N = "shop", 12
FRONT, BACK, ADMIN, BILLING = ("example/shop-frontend", "example/shop-backend",
                               "example/shop-admin", "example/billing")
IDS = {FRONT: 101, BACK: 202, ADMIN: 303, BILLING: 404}
MEMBERS = {T: frozenset({FRONT, BACK, ADMIN})}
SESSION = "7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d"
BRANCH = f"agent/task-{N}"
MODEL = "anthropic/claude-opus-5-5"


class BundleBox(Box):
    """The fake box, running a repository's command in that repository's checkout."""

    def exec(self, name, command, workdir="/workspace"):
        plan, spec = self.specs[name]
        if spec.role == "service":
            return super().exec(name, command)
        self.execs.append((name, command))
        source = next(m.source for m in spec.mounts if m.dest == workdir)
        done = sp.run(command, shell=True, cwd=source, capture_output=True, text=True)
        from dispatcher.sandbox import ExecResult
        return ExecResult(done.returncode, done.stdout + done.stderr)


class Controller(ExecutionCredentials):
    """The credential service's registry, filled by whoever launches."""

    def register_execution(self, name, repositories, purpose):
        repos = (repositories,) if isinstance(repositories, str) else tuple(repositories)
        self.executions[name] = ExecutionScope(
            T, purpose, {IDS[r]: r for r in repos})


class FakeTab:
    alive = True

    def __init__(self, commands):
        self.commands = commands

    def run(self, cmd):
        self.commands.append(cmd)
        return True


class Shop:
    """Project shop: frontend + backend + admin members, one task (#12) on the
    frontend/backend pair, one sandbox, one credential service."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.state = tmp_path, tmp_path / "state"
        self.state.mkdir()
        monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(self.state))
        self.clones = {
            FRONT: make_clone(tmp_path, "shop-frontend", {"services": {
                "redis": {"image": "redis:7", "volumes": ["redisdata:/data"]}},
                "volumes": {"redisdata": {}}}),
            BACK: make_clone(tmp_path, "shop-backend", compose()),
            ADMIN: make_clone(tmp_path, "shop-admin", compose()),
            BILLING: make_clone(tmp_path, "billing", compose())}
        self.box = BundleBox()
        self.requests = {}
        self.sandbox = TaskSandbox(self.box, tmp_path / "sandboxes", self.requests)
        self.ref = execution_ref(self.clones[FRONT], N)
        self.requests[self.ref] = SandboxRequest(
            project=T, repo=FRONT, clone=self.clones[FRONT], services=("redis",),
            setup="touch .setup-front", gate="touch .gate-front", branch=BRANCH,
            limits=Limits(2 * GIB, 2),
            members=(SandboxMember(repo=BACK, clone=self.clones[BACK], services=("postgres",),
                                   setup="touch .setup-back", gate="touch .gate-back",
                                   branch=BRANCH),))
        self.clock = Clock()
        self.app = App(self.clock, Installation(1, False, frozenset(IDS.values())))
        self.credentials = Controller(app=self.app, executions={}, clock=self.clock)
        self.commands, self.envs = [], []

        def ensure(target, tab_name, worktree, env=None, **kw):
            self.envs.append(dict(env or {}))
            return FakeTab(self.commands)
        monkeypatch.setattr(herdr.Tab, "ensure", staticmethod(ensure))

    use_sandbox = True

    def sessions(self, **kw):
        return Sessions(state_dir=self.state, clones=self.clones, credentials=self.credentials,
                        members=MEMBERS, sandbox=self.sandbox if self.use_sandbox else None, **kw)

    def plain(self):
        """Plain worktrees, for tests whose subject is not the sandbox."""
        self.use_sandbox, checkouts = False, {}
        for repo in (FRONT, BACK, ADMIN):
            wt = self.tmp / "plain" / repo.split("/")[1]
            wt.mkdir(parents=True)
            (wt / ".git").write_text("gitdir: elsewhere\n")
            checkouts[repo] = str(wt)
        return SimpleNamespace(checkouts=checkouts)

    def up(self):
        """Prepare and start the task sandbox: both checkouts, services, checks."""
        plan = self.sandbox.prepare(self.ref)
        return plan, self.sandbox.start(self.ref)

    def bundle(self, plan, repos=(FRONT, BACK), **kw):
        return TaskBundle(tuple(TaskRepository(r, plan.checkouts[r], BRANCH) for r in repos), **kw)

    def spawn(self, plan, bundle, stage="implement", sessions=None):
        (sessions or self.sessions()).spawn_stage(
            T, N, plan.checkouts[FRONT], "prompt", stage, MODEL, repo=FRONT, bundle=bundle)

    def requested_ids(self):
        return [call[1] for call in self.app.calls]


@pytest.fixture
def shop(tmp_path, monkeypatch):
    return Shop(tmp_path, monkeypatch)


def state_tokens(state_dir, name):
    """File name -> content of the token files delivered for one session."""
    return {f.name: f.read_text() for f in (Path(state_dir) / "tokens" / name).glob("gh-token*")
            if not f.name.endswith(".new")}


def assert_bundle(shop, bundle):
    """The stored bundle is the given one (short failure lines before the full compare)."""
    stored = state.read_bundle(shop.state, T, N, repo=FRONT)
    assert stored is not None, "no bundle stored"
    assert [r.repo for r in stored.repositories] == [r.repo for r in bundle.repositories]
    assert (stored.scope_revision, stored.spec_repo) == (bundle.scope_revision, bundle.spec_repo)
    assert stored == bundle


def test_s54_one_sandbox_holds_both_checkouts_services_and_scoped_credentials(shop):
    """S54: a frontend+backend task gets ONE sandbox with both checkouts and the
    verified services of both repositories under one budget, and a launch holding
    a token for each repository, never one for both."""
    plan, result = shop.up()

    assert result.ready, result.failure
    assert set(plan.checkouts) == {FRONT, BACK}
    assert len(set(plan.checkouts.values())) == 2
    assert all((Path(p) / ".git").exists() for p in plan.checkouts.values())
    assert sorted(c.service for c in plan.containers if c.role == "service") == ["postgres", "redis"]
    assert len(shop.box.plans) == 1 and {c.group for c in plan.containers} == {plan.group}
    assert (plan.memory_bytes, plan.cpus) == (2 * GIB, 2)
    assert sum(c.memory_bytes or 0 for c in plan.containers) <= 2 * GIB
    agent = next(c for c in plan.containers if c.role == "agent")
    assert {plan.checkouts[FRONT], plan.checkouts[BACK]} <= {m.source for m in agent.mounts}

    shop.spawn(plan, shop.bundle(plan))

    assert sorted(shop.requested_ids()) == [[IDS[FRONT]], [IDS[BACK]]]
    assert set(shop.credentials.executions[session_name(T, N, FRONT)].repositories) \
        == {IDS[FRONT], IDS[BACK]}
    (cmd,) = shop.commands
    assert "ghs_" not in cmd and ".config/gh" not in cmd
    # the replacement session container mounts every checkout of the bundle, not just the origin's
    agent = next(c for c in plan.containers if c.role == "agent")
    back_mount = next(m for m in agent.mounts if m.source == plan.checkouts[BACK])
    assert containers._mount(back_mount.source, back_mount.dest) in cmd
    # Each repository's token reaches the session's token directory, scoped to it.
    issued = {call[1][0]: f"ghs_{n}" for n, call in enumerate(shop.app.calls, 1)}
    tokens = state_tokens(shop.state, session_name(T, N, FRONT))
    assert tokens["gh-token"] == issued[IDS[FRONT]]
    assert tokens["gh-token.example~shop-backend"] == issued[IDS[BACK]]


def test_scoped_task_runs_checks_and_prepares_changes_in_both_repositories(shop):
    """An explicitly scoped task runs each repository's required checks in its own
    checkout and can prepare a PR change on a branch in both, from one sandbox and
    one unfinished-task allocation."""
    plan, result = shop.up()
    state.save(shop.state, make_task(issue=N, target=T, repo=FRONT, slot=0,
                                     worktree=plan.checkouts[FRONT], branch=BRANCH))

    assert result.ready, result.failure
    assert {(e.repo, e.command) for e in result.commands if e.role == "agent"} == {
        (FRONT, "touch .setup-front"), (FRONT, "touch .gate-front"),
        (BACK, "touch .setup-back"), (BACK, "touch .gate-back")}
    front, back = Path(plan.checkouts[FRONT]), Path(plan.checkouts[BACK])
    assert (front / ".gate-front").exists() and (back / ".gate-back").exists()
    assert not (front / ".gate-back").exists() and not (back / ".gate-front").exists()
    for path, name in ((front, "front"), (back, "back")):
        (path / f"{name}.txt").write_text(name)
        git("add", "-A", cwd=path)
        git("commit", "-q", "-m", f"{name} change", cwd=path)
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip() == BRANCH
    for repo in (FRONT, BACK):
        assert f"{repo.split('-')[-1]} change" not in git(
            "log", "--all", "--format=%s", cwd=shop.clones[repo])
    assert [(a.owner_kind, a.slot) for a in state.allocations(shop.state)] == [("task", 0)]
    assert len(shop.box.plans) == 1


def test_stage_transitions_and_resume_keep_scope_and_a_stage_report_cannot_widen_it(shop):
    """A later stage and a resume run on the recorded repository set and
    workspaces; a stage report or Stop ping naming another project's repository
    gains nothing."""
    plan = shop.plain()
    bundle = shop.bundle(plan, spec_repo=BACK)
    shop.spawn(plan, bundle, stage="plan")
    shop.spawn(plan, None, stage="implement")   # next stage: the controller passes no bundle
    assert_bundle(shop, bundle)

    wt = Path(plan.checkouts[FRONT])
    (wt / ".agent").mkdir(exist_ok=True)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "repositories": [BILLING, BACK, FRONT],
        "repo": BILLING, "project": "billing", "scope": [BILLING]}))
    state.save(shop.state, make_task(issue=N, target=T, repo=FRONT, worktree=str(wt)))
    handle_ping(json.dumps({
        "issue": N, "target": T, "repo": FRONT, "session_id": SESSION,
        "token": state.issue_execution_token(shop.state, T, N, FRONT),
        "repositories": [BILLING], "scope": [BILLING]}).encode(), shop.state)
    shop.app.calls.clear()
    shop.credentials.executions.clear()

    shop.sessions().resume(T, N, str(wt), "go on", MODEL, session_id=SESSION, repo=FRONT)

    assert_bundle(shop, bundle)
    assert sorted(shop.requested_ids()) == [[IDS[FRONT]], [IDS[BACK]]]
    scope = shop.credentials.executions[session_name(T, N, FRONT)]
    assert set(scope.repositories) == {IDS[FRONT], IDS[BACK]}
    with pytest.raises(CredentialDenied):
        shop.credentials.acquire(session_name(T, N, FRONT), IDS[BILLING], "task")


def test_review_artifacts_with_the_same_filename_stay_distinct_per_repository(shop):
    """The review surface tells docs/spec.md of two repositories apart and binds
    each stored artifact to the task revision it was collected under."""
    plan = shop.plain()
    bundle = shop.bundle(plan, scope_revision=3, spec_repo=FRONT)
    state.save_bundle(shop.state, T, N, bundle, repo=FRONT)
    front, back = Path(plan.checkouts[FRONT]), Path(plan.checkouts[BACK])
    for path, text in ((front, "frontend spec"), (back, "backend spec")):
        (path / "docs").mkdir()
        (path / "docs" / "spec.md").write_text(text)
    (front / ".agent").mkdir(exist_ok=True)
    (front / ".agent" / "artifacts.json").write_text(json.dumps([
        {"id": "front-spec", "name": "Spec", "path": "docs/spec.md", "repo": FRONT},
        {"id": "back-spec", "name": "Spec", "path": "docs/spec.md", "repo": BACK}]))
    task = make_task(issue=N, target=T, repo=FRONT, worktree=str(front), branch=BRANCH)

    task_artifacts.collect(shop.state, task, FRONT, publish=False)

    items = {i.id: i for i in task_artifacts.read(shop.state, T, N, repo=FRONT).items}
    assert (items["front-spec"].repo, items["back-spec"].repo) == (FRONT, BACK)
    assert items["front-spec"].digest != items["back-spec"].digest
    assert task_artifacts.content_path(shop.state, T, N, "front-spec", repo=FRONT).read_text() \
        == "frontend spec"
    assert task_artifacts.content_path(shop.state, T, N, "back-spec", repo=FRONT).read_text() \
        == "backend spec"
    assert {i.task_revision for i in items.values()} == {3}
    shown = {v.item.id: v.item for v in Sources(str(shop.state), None, None).artifacts(T, N, FRONT).items}
    assert (shown["front-spec"].repo, shown["back-spec"].repo) == (FRONT, BACK)
    assert shown["front-spec"].path == shown["back-spec"].path == "docs/spec.md"


def test_task_data_and_accounting_stay_singular_with_two_command_sets_and_services(shop):
    """Two repositories' commands and services still make one task: one
    allocation, one network, one session tab and container, one resource group."""
    plan, result = shop.up()
    state.save(shop.state, make_task(issue=N, target=T, repo=FRONT, slot=0,
                                     worktree=plan.checkouts[FRONT], branch=BRANCH))
    shop.spawn(plan, shop.bundle(plan))

    assert result.ready
    assert [(a.owner_kind, a.owner_id) for a in state.allocations(shop.state)] \
        == [("task", state.task_key(T, N, FRONT))]
    assert len(plan.networks) == 1 and len(shop.box.plans) == 1
    assert {c.network for c in plan.containers} == set(plan.networks)
    assert len(shop.commands) == 1 and len(shop.envs) == 1
    assert sorted(c.service for c in plan.containers if c.service) == ["postgres", "redis"]


def test_task_end_closes_the_one_session_of_a_multi_repository_task(shop, monkeypatch, podman):  # noqa: F811
    """Ending the bundle's session closes one tab and one container; no
    per-repository session exists to leave behind."""
    server = Herdr(monkeypatch, [session_name(T, N, FRONT)])
    plan, _ = shop.up()
    shop.spawn(plan, shop.bundle(plan))

    shop.sessions().end(T, N, repo=FRONT)

    assert len(server.closed()) == 1
    assert [c[-1] for c in podman if c[:3] == ["podman", "rm", "-f"]] == [session_name(T, N, FRONT)]


@pytest.mark.parametrize("source", ["definition", "exploration"])
def test_scope_comes_from_the_definition_or_exploration_and_membership_is_validated(shop, source):
    """Scope from the task definition or from exploration is recorded with its
    source; a repository outside the project is refused before any credential,
    tab or sandbox call."""
    plan = shop.plain()
    bundle = shop.bundle(plan, source=source)
    shop.spawn(plan, bundle)
    assert state.read_bundle(shop.state, T, N, repo=FRONT).source == source
    assert sorted(shop.requested_ids()) == [[IDS[FRONT]], [IDS[BACK]]]

    outside = TaskBundle((*bundle.repositories, TaskRepository(
        BILLING, plan.checkouts[BACK], BRANCH)), source=source)
    shop.app.calls.clear()
    shop.commands.clear()
    with pytest.raises(ScopeRefused):
        shop.spawn(plan, outside)
    assert shop.app.calls == [] and shop.commands == []
    assert_bundle(shop, bundle)


def test_definition_scope_and_spec_repository_are_recorded_when_the_controller_claims(shop, monkeypatch):
    """A task definition naming frontend and backend plus a spec repository becomes
    the task's recorded scope at claim, built from the checkouts it provisions; one
    naming a repository outside the project is refused and records nothing."""
    plan = shop.plain()
    cfg = SimpleNamespace(state_dir=str(shop.state), projects=[SimpleNamespace(
        key=T, repositories=[SimpleNamespace(repo=r) for r in MEMBERS[T]])])
    target = SimpleNamespace(name=T, repo=FRONT, sandbox=True, clone_path=str(shop.clones[FRONT]))
    provisioned, failures = [], []
    monkeypatch.setattr(main, "create_workspace",
                        lambda target, issue, **kw: provisioned.append(kw) or plan.checkouts[FRONT])
    monkeypatch.setattr(sandbox_runtime, "for_clone",
                        lambda clone: SimpleNamespace(_plan_for=lambda ref: plan))
    monkeypatch.setattr(main, "_report_provisioning_failure", lambda *a: failures.append(a))

    def claim(body, number=N):
        deps = SimpleNamespace(credentials=None, github=SimpleNamespace(
            issue_view=lambda repo, number: {"body": body}))
        return main._provision_workspace(cfg, deps, target, SimpleNamespace(number=number), False)

    assert claim(f"Build it.\nRepositories: {BACK}\nSpec repository: {BACK}\n") == plan.checkouts[FRONT]
    stored = state.read_bundle(shop.state, T, N, repo=FRONT)
    assert stored is not None, "the definition's scope was not recorded"
    assert (stored.source, stored.spec_repo) == ("definition", BACK)
    assert [r.repo for r in stored.repositories] == [FRONT, BACK]
    assert [r.worktree for r in stored.repositories] == [plan.checkouts[FRONT], plan.checkouts[BACK]]
    assert [[m.repo for m in kw["members"]] for kw in provisioned] == [[BACK]]

    assert claim(f"Repositories: {BILLING}\n", number=13) is None
    assert len(failures) == 1
    assert state.read_bundle(shop.state, T, 13, repo=FRONT) is None


def test_without_declared_scope_the_exploration_stage_holds_only_its_origin_repository(shop):
    """When scope is absent the first stage runs on its origin repository alone;
    exploration output alone grants no further repository."""
    plan = shop.plain()

    shop.spawn(plan, None, stage="spec")

    assert shop.requested_ids() == [[IDS[FRONT]]]
    assert set(shop.credentials.executions[session_name(T, N, FRONT)].repositories) == {IDS[FRONT]}
    assert state.read_bundle(shop.state, T, N, repo=FRONT) is None


def test_exploration_scope_is_established_at_the_spec_to_plan_handover(shop, monkeypatch):
    """The spec stage's done report proposing the backend is validated against the
    project and becomes the task's recorded scope when the controller hands the
    task to plan; a later report cannot change it."""
    plan = shop.plain()
    cfg = SimpleNamespace(state_dir=str(shop.state), projects=[SimpleNamespace(
        key=T, repositories=[SimpleNamespace(repo=r) for r in MEMBERS[T]])])
    target = SimpleNamespace(name=T, repo=FRONT, sandbox=True, clone_path=str(shop.clones[FRONT]))
    provisioned = []
    monkeypatch.setattr(main, "create_workspace",
                        lambda target, issue, **kw: provisioned.append(kw) or plan.checkouts[FRONT])
    monkeypatch.setattr(sandbox_runtime, "for_clone",
                        lambda clone: SimpleNamespace(_plan_for=lambda ref: plan))
    monkeypatch.setattr(main, "_end_session", lambda *a: None)
    monkeypatch.setattr(main, "_spawn_stage", lambda *a: a[3])
    task = make_task(issue=N, target=T, repo=FRONT, worktree=plan.checkouts[FRONT], stage=main.Stage.SPEC)

    def handover(repositories):
        signal = main.StageSignal("spec", "done", artifact="specs/x/spec.md", track="normal",
                                  repositories=repositories)
        turn = SimpleNamespace(cfg=cfg, deps=SimpleNamespace(credentials=None), target=target,
                               signal=signal, dry_run=False)
        return main._on_spawn_stage(turn, task, SimpleNamespace(stage=main.Stage.PLAN, tickets=0), None)

    assert handover((BACK,)) is not None
    stored = state.read_bundle(shop.state, T, N, repo=FRONT)
    assert stored is not None, "the exploration proposal was not recorded as the task's scope"
    assert stored.source == "exploration"
    assert [r.repo for r in stored.repositories] == [FRONT, BACK]
    assert [r.worktree for r in stored.repositories] == [plan.checkouts[FRONT], plan.checkouts[BACK]]
    assert [[m.repo for m in kw["members"]] for kw in provisioned] == [[BACK]]

    handover((BILLING,))
    assert len(provisioned) == 1
    assert state.read_bundle(shop.state, T, N, repo=FRONT) == stored


def _origin_and_worktree(tmp_path, name, branch):
    """A bare origin and a worktree whose branch is pushed with a spec."""
    origin, wt = tmp_path / "origins" / f"{name}.git", tmp_path / "wts" / name
    origin.mkdir(parents=True)
    sp.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    wt.mkdir(parents=True)
    git("init", "-q", "-b", branch, cwd=wt)
    git("remote", "add", "origin", str(origin), cwd=wt)
    (wt / "specs" / "x").mkdir(parents=True)
    (wt / "specs" / "x" / "spec.md").write_text(f"spec in {name}")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "spec", cwd=wt)
    git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=wt)
    return origin, wt


def test_spec_publication_uses_the_selected_repository_and_waits_when_none_is_selected(shop):
    """A spec/plan repository chosen in the task definition is where the spec is
    published; with none chosen publication waits for the user's selection
    instead of taking the first repository."""
    origins, wts = {}, {}
    for repo in (FRONT, BACK):
        origins[repo], wts[repo] = _origin_and_worktree(shop.tmp, repo.split("/")[1], BRANCH)
    bundle = TaskBundle(tuple(TaskRepository(r, str(wts[r]), BRANCH) for r in (FRONT, BACK)))
    state.save_bundle(shop.state, T, N, bundle, repo=FRONT)
    task = make_task(issue=N, target=T, repo=FRONT, worktree=str(wts[FRONT]), branch=BRANCH,
                     spec_path="specs/x/spec.md")

    task_artifacts.collect(shop.state, task, FRONT, publish=True)
    index = task_artifacts.read(shop.state, T, N, repo=FRONT)
    assert index.awaiting_spec_repo is True
    assert [i.publication for i in index.items if i.id == "spec"] in ([], [None])
    # the user is asked: the console lists the bundle's repositories as the choices
    assert Sources(str(shop.state), None, None).artifacts(T, N, FRONT).spec_repo_choices == (FRONT, BACK)

    # the answer arrives as the operator's intent and is applied by the dispatcher
    cfg = SimpleNamespace(state_dir=str(shop.state), projects=[SimpleNamespace(
        key=T, repositories=[SimpleNamespace(repo=r) for r in MEMBERS[T]])])
    main._apply_spec_repo_intent(cfg, None, intents.Intent(
        "spec-repo", T, N, {"spec_repo": BACK}, "op", "", Path("x"), FRONT))
    task_artifacts.collect(shop.state, task, FRONT, publish=True)
    assert Sources(str(shop.state), None, None).artifacts(T, N, FRONT).spec_repo_choices == ()
    index = task_artifacts.read(shop.state, T, N, repo=FRONT)
    [spec] = [i for i in index.items if i.id == "spec"]
    assert index.awaiting_spec_repo is False
    assert spec.repo == BACK and spec.publication.url.startswith(f"https://github.com/{BACK}/")


def test_resume_keeps_the_publication_repository_and_a_repository_outside_the_project_is_refused(shop):
    """Resume retains the chosen spec repository and the scope; selecting a
    repository outside the project is rejected and changes nothing."""
    plan = shop.plain()
    bundle = shop.bundle(plan, spec_repo=BACK)
    shop.spawn(plan, bundle)

    shop.sessions().resume(T, N, plan.checkouts[FRONT], "go on", MODEL,
                           session_id=SESSION, repo=FRONT)
    assert_bundle(shop, bundle)

    with pytest.raises(ScopeRefused):
        state.select_spec_repo(shop.state, T, N, BILLING, repo=FRONT, members=MEMBERS[T])
    with pytest.raises(ScopeRefused):
        shop.spawn(plan, shop.bundle(plan, spec_repo=BILLING))
    assert_bundle(shop, bundle)


def test_scope_expansion_needs_operator_confirmation_and_a_session_cannot_grant_itself(shop):
    """A session needing another member repository gets no credential for it until
    an operator-confirmed scope revision records it; no automatic late expansion."""
    plan = shop.plain()
    bundle = shop.bundle(plan)
    shop.spawn(plan, bundle)
    admin = TaskRepository(ADMIN, plan.checkouts[ADMIN], BRANCH)

    # a controller-side widening that nobody confirmed (same revision) is refused
    wider = TaskBundle((*bundle.repositories, admin), scope_revision=bundle.scope_revision)
    with pytest.raises(ScopeRefused):
        shop.spawn(plan, wider)
    shop.app.calls.clear()
    shop.sessions().resume(T, N, plan.checkouts[FRONT], "need admin too", MODEL,
                           session_id=SESSION, repo=FRONT)
    assert IDS[ADMIN] not in {i for ids in shop.requested_ids() for i in ids}

    confirmed = state.confirm_scope(shop.state, T, N, [admin], repo=FRONT, members=MEMBERS[T])
    assert confirmed.scope_revision == bundle.scope_revision + 1
    assert {r.repo for r in confirmed.repositories} == {FRONT, BACK, ADMIN}
    shop.app.calls.clear()
    shop.sessions().resume(T, N, plan.checkouts[FRONT], "go on", MODEL,
                           session_id=SESSION, repo=FRONT)
    assert IDS[ADMIN] in {i for ids in shop.requested_ids() for i in ids}


def test_approval_of_a_plan_is_void_once_the_scope_or_the_spec_repository_changes(shop):
    """An approval binds to the recorded scope: a recorded bundle, a selected
    spec repository, a secondary spec repository's plan files and a confirmed
    scope revision each change the plan revision, so a stale approval or answer
    no longer matches."""
    plan = shop.plain()
    front, back = Path(plan.checkouts[FRONT]), Path(plan.checkouts[BACK])
    (front / ".agent").mkdir()
    (front / ".agent" / "review.html").write_text(artifacts.REVIEW_PAGE_MARKER)
    (back / ".agent" / "tickets").mkdir(parents=True)
    (back / ".agent" / "tickets" / "01-one.md").write_text("one")
    task = make_task(issue=N, target=T, repo=FRONT, worktree=str(front), branch=BRANCH)

    def revision():
        return artifacts.task_plan_revision(task, ".agent/review.html").fingerprint
    plain = revision()
    assert plain
    state.save_bundle(shop.state, T, N, shop.bundle(plan), repo=FRONT)
    scoped = revision()
    assert scoped != plain
    state.select_spec_repo(shop.state, T, N, BACK, repo=FRONT, members=MEMBERS[T])
    chosen = revision()
    assert chosen != scoped
    (back / ".agent" / "tickets" / "01-one.md").write_text("two")
    edited = revision()
    assert edited != chosen
    admin = TaskRepository(ADMIN, plan.checkouts[ADMIN], BRANCH)
    state.confirm_scope(shop.state, T, N, [admin], repo=FRONT, members=MEMBERS[T])
    assert revision() != edited
