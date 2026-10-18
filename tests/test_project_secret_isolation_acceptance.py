"""Acceptance tests for ticket 08 (project-onboarding): project secrets and
runtime state stay inside one task.

Spec: specs/project-onboarding/spec.md scenarios S13, S16, S40 (R7-R10, R18).
Seam: SESSION -- Sessions.spawn_stage/resume/end, observed through the command
and tab environment a launch hands to herdr (a faked Tab), plus
containers.setup_cmd for the one-shot setup run.

Interface these tests define:
- `ExecutionCredentials.project_secrets`: {project: {ENV_NAME: "op://ref"}},
  resolved through the existing `secrets` callable. `project_env(execution_id)`
  returns the resolved {ENV_NAME: value} of the execution's own project only
  (CredentialDenied for an unknown or trusted caller).
- An unresolvable reference raises `MissingSecretReference` (a
  CredentialDenied) whose `.references` names the refs, never their values.
- A launch hands project secrets to the tab environment and names them as
  `-e NAME` in the podman command; values are never in argv, logs or files.
- Each task owns a writable runtime home under the state dir, separate from
  the shared login store `<state>/<runtime.home>`; only the shared login file
  `.credentials.json` is bound into it (one refresh lineage, no copies).
- `setup_cmd(..., project="", secret_names=())`: build caches are per project.
"""
import json
import logging
import shlex
import subprocess
from pathlib import Path

import pytest

from dispatcher import containers, herdr, workspace
from dispatcher.config import Target
from dispatcher.credential_service import CredentialService
from dispatcher.execution_credentials import (
    TRUSTED, CredentialDenied, ExecutionScope)
from dispatcher.runtimes import runtime_for
from dispatcher.sessions import Sessions, session_name
from dispatcher.state import SessionRecord, Stage, TaskState, read_session, save
from dispatcher.waitd import handle_ping
from tests.test_execution_credentials_acceptance import (
    ANALYTICS, APP_KEY, BILLING, BOARD_REF, CONTROL_REF, VALUES, build)

MODEL = "anthropic/claude-opus-5-5"
SESSION_ID = "7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d"
REFRESH_TOKEN = "sk-ant-ort01-LOGIN-REFRESH-LINEAGE"

A_REF, B_REF, B_DB_REF = ("op://vault/analytics-registry", "op://vault/billing-registry",
                          "op://vault/billing-db")
A_SECRET, B_SECRET, B_DB_SECRET = ("analytics-registry-secret-xyz",
                                   "billing-registry-secret-abc", "billing-db-secret-def")
BILLING_VALUES = (B_SECRET, B_DB_SECRET)
CENTRAL_VALUES = (VALUES[BOARD_REF], VALUES[CONTROL_REF], APP_KEY)


class Launches:
    """Every launch of the faked herdr: the tab env and the typed command."""

    def __init__(self, monkeypatch):
        self.envs, self.commands = [], []
        outer = self

        class FakeTab:
            alive = True

            def run(self, cmd):
                outer.commands.append(cmd)
                return True

        def ensure(target, tab_name, worktree, env=None, **kw):
            outer.envs.append(dict(env or {}))
            return FakeTab()
        monkeypatch.setattr(herdr.Tab, "ensure", staticmethod(ensure))


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # The shared provider login store, holding one refresh lineage.
    shared = state / "claude-home"
    shared.mkdir(parents=True)
    (shared / ".credentials.json").write_text(f'{{"refreshToken": "{REFRESH_TOKEN}"}}')
    creds, app, clock = build()
    creds.secrets = {**VALUES, A_REF: A_SECRET, B_REF: B_SECRET,
                     B_DB_REF: B_DB_SECRET}.__getitem__
    creds.project_secrets = {"analytics": {"REGISTRY_TOKEN": A_REF},
                             "billing": {"REGISTRY_TOKEN": B_REF, "BILLING_DB_URL": B_DB_REF}}
    sessions = Sessions(clones={"me/analytics": str(tmp_path / "clone-a"),
                                "me/billing": str(tmp_path / "clone-b")},
                        credentials=creds, state_dir=state)

    def worktree(name):
        wt = tmp_path / "wts" / name
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        (wt / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/{name}\n")
        return str(wt)

    def register(target, issue, repo, project):
        creds.executions[session_name(target, issue, repo)] = ExecutionScope(
            project, "task", {ANALYTICS if project == "analytics" else BILLING: repo})

    register("analytics", 7, "me/analytics", "analytics")
    register("billing", 9, "me/billing", "billing")
    launches = Launches(monkeypatch)
    return type("World", (), dict(
        state=state, shared=shared, creds=creds, sessions=sessions, launches=launches,
        worktree=staticmethod(worktree), tmp=tmp_path))


def spawn(world, target, issue, repo, wt):
    world.sessions.spawn_stage(target, issue, wt, "do the task", "implement", MODEL, repo=repo)
    return world.launches.commands[-1], world.launches.envs[-1]


def mounts(command):
    """[(source, dest, mode)] of every -v flag of a podman command line."""
    tokens = shlex.split(command) if isinstance(command, str) else list(command)
    found = []
    for i, token in enumerate(tokens[:-1]):
        if token == "-v":
            source, dest, *mode = tokens[i + 1].split(":")
            found.append((source, dest, mode[0] if mode else ""))
    return found


def home_mount(command):
    """The writable runtime-home mount of a claude session: (source, mode)."""
    (source, _, mode), = [m for m in mounts(command) if m[1] == "/root/.claude"]
    return Path(source), mode


def inside(path, root):
    return Path(path) == Path(root) or Path(root) in Path(path).parents


def all_text(*roots):
    texts = []
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file():
                texts.append(path.read_text(errors="ignore"))
    return "\n".join(texts)


def test_s13_a_session_holds_its_own_project_secrets_and_no_central_credential(world):
    command, env = spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))
    # It receives its own project's secret, by name in argv and by value in env.
    assert env["REGISTRY_TOKEN"] == A_SECRET
    assert "-e REGISTRY_TOKEN" in command
    # Never billing's secret (same variable name, other reference) nor its variables.
    assert "BILLING_DB_URL" not in env and "BILLING_DB_URL" not in command
    assert B_SECRET not in env.values() and B_DB_SECRET not in env.values()
    blob = command + "\n" + "\n".join(f"{k}={v}" for k, v in env.items())
    for forbidden in (*BILLING_VALUES, *CENTRAL_VALUES, B_REF, B_DB_REF, BOARD_REF, CONTROL_REF):
        assert forbidden not in blob
    # No value ever reaches argv, and no mount exposes the state root, the
    # other task's token directory or the dispatcher's op-token.env.
    assert A_SECRET not in command
    state = str(world.state)
    for source, _, _ in mounts(command):
        assert source != state
        assert "op-token" not in source
        assert not inside(source, world.state / "tokens" / session_name("billing", 9, "me/billing"))
    # The values are not on disk where the sibling could read them either.
    assert A_SECRET not in all_text(world.state, world.worktree("a7"))


def test_s13_the_credential_service_grants_nothing_beyond_the_tasks_project(world):
    name = session_name("analytics", 7, "me/analytics")
    creds = world.creds
    assert creds.project_env(name) == {"REGISTRY_TOKEN": A_SECRET}
    assert creds.project_env(session_name("billing", 9, "me/billing")) == {
        "REGISTRY_TOKEN": B_SECRET, "BILLING_DB_URL": B_DB_SECRET}
    # Write to billing, board, infrastructure: every route is refused.
    for repository, purpose in ((BILLING, "task"), ("me/billing", "task"),
                                ("me/1", "board"), ("me/1", "control"),
                                (CONTROL_REF, "control")):
        with pytest.raises(CredentialDenied):
            creds.acquire(name, repository, purpose)
        with pytest.raises(CredentialDenied):
            creds.refresh(name, repository, purpose)
    # The trusted caller and unknown callers have no project environment to read.
    for caller in (TRUSTED, "exec-unknown", ""):
        with pytest.raises(CredentialDenied):
            creds.project_env(caller)


def test_s16_an_unavailable_reference_is_named_refuses_the_launch_and_substitutes_nothing(
        world, caplog, capsys):
    world.creds.project_secrets["analytics"]["REGISTRY_TOKEN"] = "op://vault/missing-registry"
    world.creds.project_secrets["analytics"]["OTHER"] = A_REF  # resolvable, must not print
    caplog.set_level(logging.DEBUG)
    with pytest.raises(CredentialDenied) as raised:
        spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))
    err = raised.value
    assert type(err).__name__ == "MissingSecretReference"
    assert tuple(err.references) == ("op://vault/missing-registry",)
    assert "op://vault/missing-registry" in str(err)
    # Paused: nothing started, and no other project's secret stood in for it.
    assert world.launches.commands == [] and world.launches.envs == []
    out = str(err) + caplog.text + "".join(capsys.readouterr())
    for secret in (A_SECRET, *BILLING_VALUES, *CENTRAL_VALUES):
        assert secret not in out


def test_s40_setup_runs_do_not_share_a_writable_cache_between_projects(world):
    def setup(project, name):
        return containers.setup_cmd(
            name, world.worktree(name), "npm ci", str(world.tmp / "clone"),
            project=project, secret_names=("REGISTRY_TOKEN",))
    analytics, billing = setup("analytics", "a7"), setup("billing", "b9")
    def caches(argv):
        return {src for src, dest, mode in mounts(argv)
                if dest in ("/root/.npm", "/root/.cache") and mode != "ro"}
    assert caches(analytics) and caches(billing)
    assert not caches(analytics) & caches(billing)
    # A secret reaches the setup run by name only, never as a value in argv.
    assert ["-e", "REGISTRY_TOKEN"] == analytics[analytics.index("-e"):analytics.index("-e") + 2]
    assert A_SECRET not in " ".join(analytics)


def test_sessions_get_separate_writable_runtime_homes_and_never_the_shared_one(world):
    b_cmd, _ = spawn(world, "billing", 9, "me/billing", world.worktree("b9"))
    b_home, b_mode = home_mount(b_cmd)
    b_home.mkdir(parents=True, exist_ok=True)
    (b_home / "transcript.jsonl").write_text("BILLING-PRIVATE-CONVERSATION")
    a_cmd, _ = spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))
    a_home, a_mode = home_mount(a_cmd)
    assert "ro" not in (a_mode, b_mode)
    assert a_home != b_home
    assert not inside(a_home, b_home) and not inside(b_home, a_home)
    # Neither session writes the shared store; its history is not a mount of either.
    for command, home in ((a_cmd, a_home), (b_cmd, b_home)):
        assert not inside(home, world.shared) and not inside(world.shared, home)
        for source, _, mode in mounts(command):
            if inside(source, world.shared) and Path(source).name != ".credentials.json":
                assert mode == "ro"
    # Nothing analytics mounts reaches billing's transcript.
    for source, _, _ in mounts(a_cmd):
        assert not (Path(source) / "transcript.jsonl").exists()
        assert not inside(b_home / "transcript.jsonl", source)


def test_provider_login_is_one_shared_lineage_not_a_copy_per_task(world):
    cmds = [spawn(world, "billing", 9, "me/billing", world.worktree("b9"))[0],
            spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))[0]]
    login = world.shared / ".credentials.json"
    for command in cmds:
        home, _ = home_mount(command)
        # The login is reachable only as the single shared file: bound, not copied.
        # Writable: a refresh by any task lands in the one shared file.
        assert [(Path(src), mode) for src, dest, mode in mounts(command)
                if dest == "/root/.claude/.credentials.json"] == [(login, "")]
        copies = [p for p in home.rglob("*") if p.is_file() and not p.is_symlink()
                  and REFRESH_TOKEN in p.read_text(errors="ignore")] if home.exists() else []
        assert copies == []
    # Writable per-task state anywhere under the state dir never holds a copy.
    holders = [p for p in world.state.rglob("*") if p.is_file() and p != login
               and REFRESH_TOKEN in p.read_text(errors="ignore")]
    assert holders == []


def test_park_and_resume_keep_the_tasks_own_conversation_and_no_siblings(world, monkeypatch):
    monkeypatch.setattr("dispatcher.sessions.subprocess.run",
                        lambda args, **kw: subprocess.CompletedProcess(args, 0, "", ""))
    wt_a, wt_b = world.worktree("a7"), world.worktree("b9")
    a_cmd, _ = spawn(world, "analytics", 7, "me/analytics", wt_a)
    b_cmd, _ = spawn(world, "billing", 9, "me/billing", wt_b)
    a_home, _ = home_mount(a_cmd)
    b_home, _ = home_mount(b_cmd)
    a_home.mkdir(parents=True, exist_ok=True)
    b_home.mkdir(parents=True, exist_ok=True)
    (a_home / "conversation.jsonl").write_text("ANALYTICS-CONVERSATION")
    (b_home / "conversation.jsonl").write_text("BILLING-CONVERSATION")
    # Park: the session ends; resume continues the same recorded conversation.
    world.sessions.end("analytics", 7, "me/analytics")
    world.sessions.resume("analytics", 7, wt_a, "go on", MODEL,
                          session_id=SESSION_ID, repo="me/analytics")
    resumed = world.launches.commands[-1]
    assert f"--resume {SESSION_ID}" in resumed
    again, _ = home_mount(resumed)
    assert again == a_home
    assert (again / "conversation.jsonl").read_text() == "ANALYTICS-CONVERSATION"
    # The sibling's history is neither mounted nor visible from the resumed session.
    assert again != b_home
    assert all(not inside(b_home, src) for src, _, _ in mounts(resumed))
    assert (b_home / "conversation.jsonl").read_text() == "BILLING-CONVERSATION"


def test_secret_values_stay_out_of_logs_output_and_the_worktree(world, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    wt = world.worktree("a7")
    spawn(world, "analytics", 7, "me/analytics", wt)
    world.sessions.resume("analytics", 7, wt, "go on", MODEL,
                          session_id=SESSION_ID, repo="me/analytics")
    # The value exists, and only in the tab environment of each launch.
    assert [e["REGISTRY_TOKEN"] for e in world.launches.envs] == [A_SECRET, A_SECRET]
    out = caplog.text + "".join(capsys.readouterr())
    files = all_text(wt)  # what a generated configuration PR could commit
    for text in (out, files, *world.launches.commands):
        for secret in (A_SECRET, *BILLING_VALUES, *CENTRAL_VALUES):
            assert secret not in text


def test_a_new_task_home_copies_no_private_file_from_the_populated_shared_home(world):
    # Before the change, tasks wrote into the shared home: history and config
    # of earlier tasks sit there beside the login file.
    (world.shared / "history.jsonl").write_text("BILLING-PRIVATE-HISTORY")
    (world.shared / ".claude.json").write_text(f'{{"mcpToken": "{B_SECRET}"}}')
    (world.shared / "projects").mkdir()
    (world.shared / "projects" / "billing.jsonl").write_text("BILLING-PRIVATE-TRANSCRIPT")
    command, _ = spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))
    home, _ = home_mount(command)
    seen = all_text(home) if home.exists() else ""
    for private in ("BILLING-PRIVATE-HISTORY", "BILLING-PRIVATE-TRANSCRIPT", B_SECRET, REFRESH_TOKEN):
        assert private not in seen


def test_a_codex_conversation_in_the_isolated_home_is_found_and_recorded_by_the_ping(world):
    target, issue, session_id = "analytics", 7, "01a11028-9f0c-75f3-8b77-a5b5e6dfb884"
    wt = world.worktree("a7")
    save(world.state, TaskState(issue=issue, target=target, stage=Stage.REVIEW, slot=0,
                                worktree=wt, branch="agent/task-7", title="t",
                                updated_at="2026-10-06T09:00:00+00:00"))
    home = containers.task_home(world.state, session_name(target, issue, ""),
                                runtime_for("openai/gpt-5.5"))
    rollouts = home / "sessions" / "2026" / "10" / "06"
    rollouts.mkdir(parents=True)
    (rollouts / f"rollout-2026-10-06T08-16-00-{session_id}.jsonl").write_text(json.dumps(
        {"type": "session_meta", "payload": {"id": session_id, "cwd": wt, "source": "cli"}}) + "\n")
    handle_ping(json.dumps({"issue": issue, "target": target, "runtime": "codex",
                            "session_id": session_id}).encode(), world.state)
    assert read_session(world.state, target, issue) == SessionRecord(session_id, "review")


def test_real_setup_runs_of_two_projects_share_no_writable_cache(world, monkeypatch):
    runs = []

    def record(argv, **kw):
        if isinstance(argv, list) and "podman" in argv[0]:
            runs.append(argv)
        elif argv[:3] == ["git", "worktree", "add"]:
            Path(argv[-2] if argv[3] == "-b" else argv[-1]).mkdir(parents=True, exist_ok=True)
        return ""
    monkeypatch.setattr(workspace, "_sh", record)
    monkeypatch.setattr(workspace, "_branch_exists", lambda *a, **k: False)
    for name in ("analytics", "billing"):
        clone = world.tmp / f"clone-{name}"
        (clone / ".git").mkdir(parents=True, exist_ok=True)
        workspace.create_workspace(Target(
            name=name, repo=f"me/{name}", clone_path=str(clone),
            worktrees_path=str(world.tmp / f"wts-{name}"), rank_cmd="", project_number=1,
            project_owner="me", status_field_id="", status_ready_option_id="",
            status_in_progress_option_id="", setup_cmd="npm ci", gate_cmd="make gate"), 5)
    assert len(runs) == 2
    caches = [{src for src, dest, mode in mounts(argv)
               if dest in ("/root/.npm", "/root/.cache") and mode != "ro"} for argv in runs]
    assert all(caches) and not caches[0] & caches[1]


def test_the_production_credential_client_refuses_a_launch_when_project_secrets_are_unresolvable(
        world):
    operations = []

    def transport(operation, body):  # a service that has no project-secret operation
        operations.append(operation)
        if operation == "executions/register":
            return {}
        if operation == "refresh":
            return {"token": "ghs_x", "repository_id": "1", "permissions": {},
                    "expires_at": 9e9}
        raise CredentialDenied(f"no such operation: {operation}")
    service = CredentialService("/nonexistent.sock", {"me/analytics": "analytics"})
    service._request = transport
    world.sessions.credentials = service
    with pytest.raises(CredentialDenied):
        spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))
    # Fail closed: the launch is refused before anything starts.
    assert world.launches.commands == [] and world.launches.envs == []
    assert set(operations) - {"executions/register", "refresh"}


def _provision(world, monkeypatch, name, issue, credentials):
    """Production create_workspace of `name`, recording every podman run and the
    environment the launcher gave it. Interface: `credentials=` (as Deps and
    Sessions take them); a setup run's values travel in `_sh(..., env=...)`."""
    runs = []

    def record(argv, **kw):
        if isinstance(argv, list) and "podman" in argv[0]:
            runs.append((argv, dict(kw.get("env") or {})))
        elif argv[:3] == ["git", "worktree", "add"]:
            Path(argv[-2] if argv[3] == "-b" else argv[-1]).mkdir(parents=True, exist_ok=True)
        return ""
    monkeypatch.setattr(workspace, "_sh", record)
    monkeypatch.setattr(workspace, "_branch_exists", lambda *a, **k: False)
    # whichever id the setup execution runs under is the task's own project
    world.creds.executions[f"task-{name}-{issue}-setup"] = world.creds.executions[
        session_name(name, issue, f"me/{name}")]
    clone = world.tmp / f"clone-{name}"
    (clone / ".git").mkdir(parents=True, exist_ok=True)
    target = Target(
        name=name, repo=f"me/{name}", clone_path=str(clone),
        worktrees_path=str(world.tmp / f"wts-{name}"), rank_cmd="", project_number=1,
        project_owner="me", status_field_id="", status_ready_option_id="",
        status_in_progress_option_id="", setup_cmd="npm ci", gate_cmd="make gate")
    return runs, lambda: workspace.create_workspace(target, issue, credentials=credentials)


def test_the_real_setup_run_receives_its_projects_secrets_by_name_and_value_by_environment(
        world, monkeypatch):
    runs, create = _provision(world, monkeypatch, "billing", 9, world.creds)
    create()
    (argv, env), = runs
    # Both of billing's secrets reach the install, and nobody else's.
    assert env == {"REGISTRY_TOKEN": B_SECRET, "BILLING_DB_URL": B_DB_SECRET}
    names = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-e"]
    assert sorted(names) == ["BILLING_DB_URL", "REGISTRY_TOKEN"]
    blob = " ".join(argv)
    for value in (*BILLING_VALUES, A_SECRET, *CENTRAL_VALUES):
        assert value not in blob


def test_the_real_setup_run_is_refused_naming_the_missing_reference_before_anything_starts(
        world, monkeypatch, caplog, capsys):
    world.creds.project_secrets["analytics"]["REGISTRY_TOKEN"] = "op://vault/missing-registry"
    caplog.set_level(logging.DEBUG)
    runs, create = _provision(world, monkeypatch, "analytics", 7, world.creds)
    with pytest.raises(CredentialDenied) as raised:
        create()
    assert tuple(raised.value.references) == ("op://vault/missing-registry",)
    assert runs == []  # paused: no setup container ran, with no substitute secret
    out = str(raised.value) + caplog.text + "".join(capsys.readouterr())
    for secret in (A_SECRET, *BILLING_VALUES, *CENTRAL_VALUES):
        assert secret not in out


def _transcript(cwd, text):
    """A realistic CLI conversation: JSONL whose records name the working directory."""
    return "\n".join(json.dumps(r) for r in (
        {"type": "summary", "summary": text},
        {"type": "user", "cwd": cwd, "sessionId": SESSION_ID,
         "message": {"role": "user", "content": text}})) + "\n"


def _legacy(world, session_id, content):
    legacy = world.shared / "projects" / "-workspace"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / f"{session_id}.jsonl").write_text(content)


def _resume_home(world, wt, session_id=SESSION_ID):
    world.sessions.resume("analytics", 7, wt, "go on", MODEL,
                          session_id=session_id, repo="me/analytics")
    home, _ = home_mount(world.launches.commands[-1])
    return home


def test_resume_migrates_the_tasks_own_legacy_conversation_and_none_of_its_siblings(world):
    # Before task homes, every conversation sat in the shared home.
    wt, other = world.worktree("a7"), world.worktree("b9")
    own = _transcript(wt, "ANALYTICS-LEGACY-CONVERSATION")
    _legacy(world, SESSION_ID, own)
    sibling = "11111111-2222-4333-8444-555555555555"
    _legacy(world, sibling, _transcript(other, "BILLING-LEGACY-CONVERSATION"))
    (world.shared / "history.jsonl").write_text("BILLING-LEGACY-HISTORY")
    home = _resume_home(world, wt)
    # The conversation being resumed is there for the CLI to continue ...
    assert (home / "projects" / "-workspace" / f"{SESSION_ID}.jsonl").read_text() == own
    # ... and nothing of the siblings came along.
    seen = all_text(home)
    assert "BILLING-LEGACY-CONVERSATION" not in seen and "BILLING-LEGACY-HISTORY" not in seen


def test_resume_never_migrates_a_conversation_recorded_in_another_tasks_worktree(world):
    # A task that reports a sibling's exact session ID gets no sibling history.
    wt, other = world.worktree("a7"), world.worktree("b9")
    _legacy(world, SESSION_ID, _transcript(other, "BILLING-PRIVATE-CONVERSATION"))
    home = _resume_home(world, wt)
    assert "BILLING-PRIVATE-CONVERSATION" not in all_text(home)


@pytest.mark.parametrize("content", [
    "BILLING-PRIVATE-CONVERSATION",  # not a conversation record at all
    json.dumps({"type": "user", "message": "BILLING-PRIVATE-CONVERSATION"}) + "\n",  # no cwd
    "{not json BILLING-PRIVATE-CONVERSATION\n",  # malformed
])
def test_resume_never_migrates_a_conversation_with_no_verifiable_owner(world, content):
    _legacy(world, SESSION_ID, content)
    home = _resume_home(world, world.worktree("a7"))
    assert "BILLING-PRIVATE-CONVERSATION" not in all_text(home)


def test_the_codex_login_is_also_one_shared_writable_file_seen_by_every_task(world):
    shared = world.state / "codex-home"
    shared.mkdir()
    auth = shared / "auth.json"
    auth.write_text('{"refresh_token": "CODEX-LINEAGE-1"}')
    codex = "openai/gpt-5.5"
    for target, issue, repo, name in (("analytics", 7, "me/analytics", "a7"),
                                      ("billing", 9, "me/billing", "b9")):
        world.sessions.spawn_stage(target, issue, world.worktree(name), "do", "implement",
                                   codex, repo=repo)
    for command in world.launches.commands:
        bound = [(Path(s), m) for s, d, m in mounts(command) if d == "/root/.codex/auth.json"]
        assert bound == [(auth, "")]  # the one shared file, writable, never a copy
    # A refresh written through one task's binding is what every other task reads.
    auth.write_text('{"refresh_token": "CODEX-LINEAGE-2"}')
    assert all(Path(s).read_text() == '{"refresh_token": "CODEX-LINEAGE-2"}'
               for c in world.launches.commands for s, d, _ in mounts(c)
               if d == "/root/.codex/auth.json")
    copies = [p for p in world.state.rglob("*") if p.is_file() and p != auth
              and "CODEX-LINEAGE" in p.read_text(errors="ignore")]
    assert copies == []


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_setup_output_never_persists_or_propagates_a_project_secret(world, monkeypatch, outcome):
    """The real setup run echoes its environment on stdout and stderr; neither
    the setup log nor the raised error may keep a value."""
    def run(argv, **kw):
        if isinstance(argv, list) and "podman" in argv[0]:
            leak = "\n".join(f"{k}={v}" for k, v in kw["env"].items()
                             if v in BILLING_VALUES)
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(argv, 1, output=leak.encode(), stderr=leak.encode())
            return subprocess.CompletedProcess(argv, 0 if outcome == "success" else 1,
                                               leak, leak)
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[-2] if argv[3] == "-b" else argv[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(workspace.subprocess, "run", run)
    monkeypatch.setattr(workspace, "_branch_exists", lambda *a, **k: False)
    world.creds.executions["task-billing-9-setup"] = world.creds.executions[
        session_name("billing", 9, "me/billing")]
    clone = world.tmp / "clone-billing"
    (clone / ".git").mkdir(parents=True, exist_ok=True)
    target = Target(
        name="billing", repo="me/billing", clone_path=str(clone),
        worktrees_path=str(world.tmp / "wts-billing"), rank_cmd="", project_number=1,
        project_owner="me", status_field_id="", status_ready_option_id="",
        status_in_progress_option_id="", setup_cmd="npm ci", gate_cmd="make gate")
    raised = ""
    try:
        workspace.create_workspace(target, 9, credentials=world.creds)
    except Exception as exc:  # noqa: BLE001 - whatever the failure, it must stay clean
        raised = " ".join(str(a) for a in (exc, *exc.args, getattr(exc, "output", ""),
                                           getattr(exc, "stdout", ""), getattr(exc, "stderr", "")))
    assert (outcome == "success") == (raised == "")
    persisted = all_text(world.tmp / "wts-billing")
    for secret in BILLING_VALUES:
        assert secret not in persisted
        assert secret not in raised
