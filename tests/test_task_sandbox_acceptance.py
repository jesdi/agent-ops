"""Acceptance tests for ticket 09 (project-onboarding): confirmed commands and
Compose services run in owned task sandboxes.

Spec: specs/project-onboarding/spec.md scenarios S17, S18, S38, S39, S41, S59
(R10, R18, R19). Seam: SANDBOX -- TaskSandbox.prepare/start (park/resume/cleanup
are not exercised here), observed through a fake rootless box runtime (`Engine`,
see dispatcher/sandbox.py) and real Git checkouts.

Interface these tests define (dispatcher/sandbox.py): `SandboxRequest` per
execution ref, `ServicePlan`/`ContainerSpec`/`Mount` handed to `engine.create`,
`SandboxBlocked(.blockers)`, `StartResult(ready, commands, failure)`.
The fake box runs agent commands for real in the checkout it mounted at
/workspace, and keeps service data in the volume mounted at the data path.
"""
import subprocess
from pathlib import Path

import pytest
import yaml

from dispatcher.sandbox import (
    EngineError, ExecResult, Limits, SandboxBlocked, SandboxRequest, TaskSandbox)

GIB = 1 << 30
DATA_PATHS = ("/var/lib/postgresql/data", "/data")


def compose(**extra_postgres):
    return {"services": {
        "postgres": {"image": "postgres:16", "volumes": ["pgdata:/var/lib/postgresql/data"],
                     **extra_postgres},
        "redis": {"image": "redis:7", "volumes": ["redisdata:/data"]},
        "mailhog": {"image": "mailhog/mailhog"}},
        "volumes": {"pgdata": {}, "redisdata": {}}}


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                          "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                                          "HOME": str(cwd)}).stdout


def make_clone(root, name, compose_doc):
    clone = Path(root) / "clones" / name
    clone.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=clone)
    if compose_doc is not None:
        (clone / "compose.yaml").write_text(yaml.safe_dump(compose_doc))
    (clone / "README.md").write_text(name)
    git("add", "-A", cwd=clone)
    git("commit", "-q", "-m", "init", cwd=clone)
    return str(clone)


class Box:
    """Fake rootless box runtime."""

    def __init__(self):
        self.plans, self.specs, self.running, self.execs = {}, {}, set(), []
        self.data, self.fail_start = {}, set()

    def create(self, plan):
        self.plans[plan.ref] = plan
        for c in plan.containers:
            self.specs[c.name] = (plan, c)

    def start(self, plan, name):
        if self.specs[name][1].service in self.fail_start:
            raise EngineError(f"{name} did not start")
        self.running.add(name)

    def stop(self, plan, name):
        self.running.discard(name)

    def remove(self, plan):
        self.plans.pop(plan.ref, None)

    def volume_of(self, name):
        return next(m.source for m in self.specs[name][1].mounts if m.dest in DATA_PATHS)

    def exec(self, name, command):
        plan, spec = self.specs[name]
        self.execs.append((name, command))
        if spec.role == "service":
            rows = self.data.setdefault(self.volume_of(name), [])
            if command.startswith("sql insert "):
                rows.append(command[len("sql insert "):])
            return ExecResult(0, "\n".join(rows))
        workspace = next(m.source for m in spec.mounts if m.dest == "/workspace")
        done = subprocess.run(command, shell=True, cwd=workspace, capture_output=True, text=True)
        return ExecResult(done.returncode, done.stdout + done.stderr)

    def container(self, ref, service=""):
        return next(c.name for c in self.plans[ref].containers if c.service == service)

    def reachable(self, a, b):
        return self.specs[a][1].network == self.specs[b][1].network


@pytest.fixture
def world(tmp_path):
    box = Box()
    requests = {}

    def add(ref, project, doc=None, **kw):
        clone = make_clone(tmp_path / ref, project, compose(**{}) if doc is None else doc)
        requests[ref] = SandboxRequest(
            project=project, repo=f"me/{project}", clone=clone,
            services=kw.pop("services", ("postgres", "redis")),
            setup=kw.pop("setup", "touch .setup-done"),
            gate=kw.pop("gate", "touch .gate-done"), **kw)
        return ref

    sandbox = TaskSandbox(box, tmp_path / "state", requests)
    return type("World", (), dict(box=box, sandbox=sandbox, add=staticmethod(add),
                                  requests=requests, tmp=tmp_path))


def up(world, ref):
    plan = world.sandbox.prepare(ref)
    return plan, world.sandbox.start(ref)


def test_edited_gate_runs_with_confirmed_services_and_is_reported(world):
    """S17: the operator edited the proposed gate; the edited command runs, with
    only the confirmed services, and results name what actually ran."""
    ref = world.add("analytics-1", "analytics", gate="echo edited-gate > gate.out",
                    doc=compose(environment={"POSTGRES_PASSWORD": "pw"},
                                command="postgres -c fsync=off",
                                healthcheck={"test": ["CMD", "pg_isready"]}))
    plan, result = up(world, ref)

    assert result.ready and result.failure is None
    assert sorted(c.service for c in plan.containers if c.role == "service") == ["postgres", "redis"]
    assert {world.box.specs[n][1].service for n in world.box.running} == {"", "postgres", "redis"}
    agent = world.box.container(ref)
    assert [(e.role, e.container, e.command) for e in result.commands] == [
        ("agent", agent, "touch .setup-done"), ("agent", agent, "echo edited-gate > gate.out")]
    assert [(n, c) for n, c in world.box.execs] == [(e.container, e.command) for e in result.commands]
    assert (Path(plan.checkout) / "gate.out").read_text() == "edited-gate\n"
    # the confirmed service definitions reach the runtime, not only their names
    postgres = world.box.specs[world.box.container(ref, "postgres")][1]
    redis = world.box.specs[world.box.container(ref, "redis")][1]
    assert (postgres.image, redis.image) == ("postgres:16", "redis:7")
    assert dict(postgres.environment) == {"POSTGRES_PASSWORD": "pw"}
    assert postgres.command == "postgres -c fsync=off"
    assert postgres.healthcheck == {"test": ["CMD", "pg_isready"]}


def test_missing_service_definition_blocks_preparation(world):
    """S18: setup needs PostgreSQL but compose.yaml has no usable definition."""
    ref = world.add("analytics-1", "analytics", doc={"services": {"redis": {"image": "redis:7"}}})

    with pytest.raises(SandboxBlocked) as blocked:
        world.sandbox.prepare(ref)

    [blocker] = [b for b in blocked.value.blockers if b.service == "postgres"]
    assert blocker.kind == "missing-service-definition" and blocker.detail
    assert world.box.plans == {} and world.box.running == set()


@pytest.mark.parametrize("case", ["no-compose-file", "empty-definition", "null-definition"])
def test_absent_or_unusable_service_definition_blocks_preparation(world, case):
    """S18: a missing compose file, or a selected service whose definition is
    empty, is a concrete blocker naming the service, never readiness."""
    doc = {"empty-definition": {"services": {"postgres": {}, "redis": {"image": "redis:7"}}},
           "null-definition": {"services": {"postgres": None, "redis": {"image": "redis:7"}}},
           }.get(case)
    ref = world.add("analytics-1", "analytics", doc=doc or compose())
    if case == "no-compose-file":
        (Path(world.requests[ref].clone) / "compose.yaml").unlink()

    with pytest.raises(SandboxBlocked) as blocked:
        world.sandbox.prepare(ref)

    [blocker] = [b for b in blocked.value.blockers if b.service == "postgres"]
    assert blocker.kind == "missing-service-definition" and blocker.detail
    assert world.box.plans == {} and world.box.running == set()


@pytest.mark.parametrize("case", ["no-image", "malformed-memory", "anonymous-volume",
                                  "negative-memory", "nan-cpus", "scalar-volume-definition"])
def test_nonempty_but_unusable_service_definition_is_a_service_blocker(world, case):
    """S18: a selected service that is non-empty yet cannot run (no image,
    malformed limit, anonymous volume) is a concrete blocker naming it, never
    readiness and never a raw exception."""
    doc = compose()
    doc["services"]["postgres"] = {
        "no-image": {"command": "postgres"},
        "malformed-memory": {"image": "postgres:16",
                             "deploy": {"resources": {"limits": {"memory": "not-memory"}}}},
        "anonymous-volume": {"image": "postgres:16", "volumes": ["/var/lib/postgresql/data"]},
        "negative-memory": {"image": "postgres:16",
                            "deploy": {"resources": {"limits": {"memory": "-512m"}}}},
        "nan-cpus": {"image": "postgres:16",
                     "deploy": {"resources": {"limits": {"cpus": "nan"}}}},
        "scalar-volume-definition": {"image": "postgres:16",
                                     "volumes": ["pgdata:/var/lib/postgresql/data"]},
    }[case]
    if case == "scalar-volume-definition":
        doc["volumes"]["pgdata"] = 42
    ref = world.add("analytics-1", "analytics", doc=doc)

    with pytest.raises(SandboxBlocked) as blocked:
        world.sandbox.prepare(ref)

    [blocker] = [b for b in blocked.value.blockers if b.service == "postgres"]
    assert blocker.kind in ("missing-service-definition", "incompatible-compose") and blocker.detail
    assert world.box.plans == {} and world.box.running == set()


@pytest.mark.parametrize("case", ["user", "working-dir", "read-only-short", "read-only-long",
                                  "bind-mount-long", "replicas"])
def test_accepted_runtime_and_mount_settings_are_kept_or_rejected(world, case):
    """S17: a confirmed user, working directory or read-only volume is carried
    to the runtime as confirmed, or the service is blocked; it is never
    silently dropped or weakened."""
    extra = {"user": {"user": "1001:1001"}, "working-dir": {"working_dir": "/app"},
             "read-only-short": {"volumes": ["pgdata:/var/lib/postgresql/data:ro"]},
             "read-only-long": {"volumes": [{"type": "volume", "source": "pgdata",
                                             "target": "/var/lib/postgresql/data",
                                             "read_only": True}]},
             "bind-mount-long": {"volumes": [{"type": "bind", "source": "pgdata",
                                              "target": "/var/lib/postgresql/data"}]},
             "replicas": {"deploy": {"replicas": 3}}}[case]
    ref = world.add("analytics-1", "analytics", doc=compose(**extra))
    try:
        plan = world.sandbox.prepare(ref)
    except SandboxBlocked as blocked:
        assert [b.kind for b in blocked.blockers if b.service == "postgres"] == ["incompatible-compose"]
        assert world.box.plans == {}
        return

    postgres = world.box.specs[world.box.container(ref, "postgres")][1]
    if case == "user":
        assert getattr(postgres, "user", None) == "1001:1001"
    elif case == "working-dir":
        assert getattr(postgres, "working_dir", None) == "/app"
    elif case == "bind-mount-long":
        # a confirmed bind must not be rewritten into a sandbox-owned named volume
        [data] = [m for m in postgres.mounts if m.dest == "/var/lib/postgresql/data"]
        assert data.source not in plan.volumes
    elif case == "replicas":
        assert len([c for c in plan.containers if c.service == "postgres"]) == 3
    else:
        [data] = [m for m in postgres.mounts if m.dest == "/var/lib/postgresql/data"]
        assert data.mode == "ro"


def test_failing_gate_is_reported_not_ready(world):
    """S18: a failing confirmed gate command never yields readiness."""
    ref = world.add("analytics-1", "analytics", gate="exit 3")
    _, result = up(world, ref)

    assert not result.ready
    assert (result.failure.command, result.failure.exit_code) == ("exit 3", 3)
    assert result.failure.service == ""
    assert result.commands[-1].exit_code == 3


def test_service_start_failure_names_the_service_and_skips_checks(world):
    """A supporting service that cannot start is reported by name; no check
    runs against a missing service and readiness is not claimed."""
    ref = world.add("analytics-1", "analytics")
    world.box.fail_start.add("redis")
    _, result = up(world, ref)

    assert not result.ready and result.failure.service == "redis"
    assert world.box.execs == []


def test_sibling_tasks_keep_their_own_checkout_and_database(world):
    """S38: two analytics tasks and a billing task each own checkout, database
    and private network; changes in task #1 reach none of the others."""
    refs = [world.add("analytics-1", "analytics"), world.add("analytics-2", "analytics"),
            world.add("billing-1", "billing")]
    plans = [world.sandbox.prepare(r) for r in refs]
    for r in refs:
        world.sandbox.start(r)
    a1, a2, b1 = refs

    world.box.exec(world.box.container(a1), "echo changed > edited.txt")
    world.box.exec(world.box.container(a1, "postgres"), "sql insert invoice-1")

    assert (Path(plans[0].checkout) / "edited.txt").exists()
    assert not (Path(plans[1].checkout) / "edited.txt").exists()
    assert not (Path(plans[2].checkout) / "edited.txt").exists()
    assert len({p.checkout for p in plans}) == 3
    for other in (a2, b1):
        assert world.box.exec(world.box.container(other, "postgres"), "sql list").output == ""
    assert world.box.exec(world.box.container(a1, "postgres"), "sql list").output == "invoice-1"
    for i, j in ((0, 1), (0, 2), (1, 2)):
        assert set(plans[i].networks).isdisjoint(plans[j].networks)
        assert set(plans[i].volumes).isdisjoint(plans[j].volumes)
        for ci in plans[i].containers:
            for cj in plans[j].containers:
                assert not world.box.reachable(ci.name, cj.name)
    assert world.box.reachable(world.box.container(a1), world.box.container(a1, "postgres"))


def test_repository_commands_run_only_inside_assigned_sandbox_containers(world):
    """S39: install, gate and service all run in the task's own containers; the
    sandbox holds no host-control or broad host mount."""
    ref = world.add("analytics-1", "analytics", setup="make install", gate="make check")
    (Path(world.requests[ref].clone) / "Makefile").write_text(
        "install:\n\ttouch installed\ncheck:\n\ttest -f installed\n")
    git("add", "-A", cwd=world.requests[ref].clone)
    git("commit", "-q", "-m", "make", cwd=world.requests[ref].clone)
    plan, result = up(world, ref)

    assert result.ready
    own = {c.name for c in plan.containers}
    assert {name for name, _ in world.box.execs} <= own
    # both commands really traversed the assigned agent container (an empty log proves nothing)
    agent = world.box.container(ref)
    assert world.box.execs == [(agent, "make install"), (agent, "make check")]
    assert [(e.container, e.command) for e in result.commands] == world.box.execs
    assert (Path(plan.checkout) / "installed").exists()
    for c in plan.containers:
        assert not c.privileged
        for m in c.mounts:
            if m.source.startswith("/"):
                assert m.source == plan.checkout or Path(m.source).is_relative_to(plan.checkout), m
            else:
                assert m.source in plan.volumes, m
            assert "docker.sock" not in m.source and "podman.sock" not in m.source


@pytest.mark.parametrize("case", ["host-bind", "control-socket", "sibling-volume",
                                  "privileged", "host-network", "external-volume",
                                  "host-backed-volume"])
def test_isolation_breaking_compose_is_blocked_not_granted(world, case):
    """S59: Compose asking for a host resource or another task's volume yields a
    concrete blocker; nothing is created and no access is granted to pass."""
    other = world.add("analytics-2", "analytics")
    sibling_volume = world.sandbox.prepare(other).volumes[0]
    breaking = {
        "host-bind": {"volumes": ["/etc:/host-etc"]},
        "control-socket": {"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]},
        "sibling-volume": {"volumes": [f"{sibling_volume}:/var/lib/postgresql/data"]},
        "privileged": {"privileged": True},
        "host-network": {"network_mode": "host"},
    }.get(case, {})
    doc = compose(**breaking)
    if case == "external-volume":
        doc["volumes"]["pgdata"] = {"external": True, "name": sibling_volume}
    if case == "host-backed-volume":
        doc["volumes"]["pgdata"] = {"driver": "local", "driver_opts": {
            "type": "none", "o": "bind", "device": "/etc"}}
    ref = world.add("analytics-1", "analytics", doc=doc)

    with pytest.raises(SandboxBlocked) as blocked:
        world.sandbox.prepare(ref)

    [blocker] = [b for b in blocked.value.blockers if b.service == "postgres"]
    assert blocker.kind == "incompatible-compose" and blocker.detail
    assert ref not in world.box.plans and world.box.running == set()
    assert set(world.box.plans) == {other}


def test_git_metadata_is_task_private_and_clone_tree_is_not_mounted(world):
    """Git works in the checkout without mounting the shared clone: commits are
    private to the task and no mount exposes the clone or sibling workspaces."""
    a1, a2 = world.add("analytics-1", "analytics"), world.add("analytics-2", "analytics")
    plans = [world.sandbox.prepare(a1), world.sandbox.prepare(a2)]
    clone = world.requests[a1].clone

    c1 = Path(plans[0].checkout)
    assert (c1 / ".git").is_dir()
    (c1 / "feature.txt").write_text("x")
    git("add", "-A", cwd=c1)
    git("commit", "-q", "-m", "task one", cwd=c1)

    assert "task one" in git("log", "--format=%s", cwd=c1)
    assert "task one" not in git("log", "--format=%s", cwd=plans[1].checkout)
    assert "task one" not in git("log", "--all", "--format=%s", cwd=clone)
    for plan in plans:
        for c in plan.containers:
            for m in c.mounts:
                assert not (m.source.startswith("/") and Path(m.source).is_relative_to(clone)), m
        agent = next(c for c in plan.containers if c.role == "agent")
        assert [m.source for m in agent.mounts if m.source.startswith("/")] == [plan.checkout]


def test_agent_and_services_are_accounted_in_one_resource_group(world):
    """S41: the agent and Compose services share one 2 GiB / 2 CPU allowance; the
    sum of per-container caps cannot exceed it, and a group is the proof."""
    ref = world.add("analytics-1", "analytics", limits=Limits(2 * GIB, 2),
                    agent_demand=Limits(1 * GIB, 1),
                    doc=compose(deploy={"resources": {"limits": {"memory": "512m", "cpus": "0.5"}}}))
    plan = world.sandbox.prepare(ref)

    assert (plan.memory_bytes, plan.cpus) == (2 * GIB, 2)
    assert {c.group for c in plan.containers} == {plan.group}
    capped = [c for c in plan.containers if c.memory_bytes is not None]
    assert sum(c.memory_bytes for c in capped) <= 2 * GIB
    assert sum(c.cpus or 0 for c in plan.containers) <= 2
    assert all((c.memory_bytes or 0) <= 2 * GIB for c in plan.containers)


def test_combined_demand_over_the_allowance_is_reported(world):
    """S41: each container fits on its own but together they exceed 2 GiB; the
    excess is not silently granted."""
    ref = world.add("analytics-1", "analytics", limits=Limits(2 * GIB, 2),
                    agent_demand=Limits(1 * GIB, 1),
                    doc=compose(deploy={"resources": {"limits": {"memory": "1536m"}}}))

    with pytest.raises(SandboxBlocked) as blocked:
        world.sandbox.prepare(ref)

    assert {b.kind for b in blocked.value.blockers} == {"resource-allowance"}
    assert world.box.plans == {}


def test_checkout_directory_names_the_project_and_the_task(world):
    """Two projects' #42 get distinct checkouts that say whose task each is."""
    plans = [world.sandbox.prepare(world.add(f"{p}#42", p)) for p in ("alpha", "beta")]

    names = [Path(plan.checkout).name for plan in plans]
    assert names[0].startswith("alpha-task-42-") and names[1].startswith("beta-task-42-")
    assert names[0] != names[1]
