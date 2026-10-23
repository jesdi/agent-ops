"""Task sandboxes: seam SANDBOX -- `TaskSandbox.prepare/start/park/resume/cleanup(execution_ref)`.

- `requests` maps an execution ref to the `SandboxRequest` recorded for it
  (project, repository clone, confirmed Compose services and commands, the
  combined `Limits`).
- `engine` is the rootless box runtime edge: `create(plan)`, `start(plan, name)`
  (raises `EngineError`), `exec(name, command) -> ExecResult`, `stop(plan, name)`,
  `remove(plan)`. Every repository command runs through `engine.exec` in a
  container of the task's own `ServicePlan`.
- `prepare` makes the task-private checkout and returns the `ServicePlan`
  (also handed to `engine.create`); it raises `SandboxBlocked` with concrete
  `Blocker`s instead of weakening isolation. `start` starts the confirmed
  services, runs setup (and the gate when `run_gate`) and reports a
  `StartResult`; `ready` is True only when everything succeeded.
"""
from __future__ import annotations

import fcntl
import hashlib
import math
import os
import re
import tempfile
from contextlib import contextmanager
from functools import wraps
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Mapping, Protocol

import yaml

WORKSPACE = "/workspace"   # where the origin repository's checkout is mounted


@dataclass(frozen=True)
class Limits:
    memory_bytes: int
    cpus: float


@dataclass(frozen=True)
class SandboxMember:
    """A further required repository of the same task sandbox."""
    repo: str
    clone: str
    services: tuple[str, ...] = ()
    setup: str = ""
    gate: str = ""
    compose: str = "compose.yaml"
    branch: str = ""


@dataclass(frozen=True)
class SandboxRequest:
    project: str
    repo: str
    clone: str                      # the project's shared clone on the host
    services: tuple[str, ...] = ()  # operator-confirmed Compose services
    setup: str = ""
    gate: str = ""
    limits: Limits = Limits(2 << 30, 2)   # combined agent + services allowance
    agent_demand: Limits | None = None    # what the agent container asks for
    compose: str = "compose.yaml"         # path inside the checkout
    run_gate: bool = True
    branch: str = ""                     # created only before the checkout is exposed
    members: tuple[SandboxMember, ...] = ()   # other required repositories, same sandbox


@dataclass(frozen=True)
class Mount:
    source: str                     # absolute host path, or an owned volume name
    dest: str
    mode: str = "rw"


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    role: str                       # "agent" | "service"
    service: str                    # Compose service name ("" for the agent)
    mounts: tuple[Mount, ...]
    network: str
    group: str                      # the one resource group it is accounted in
    privileged: bool = False
    memory_bytes: int | None = None
    cpus: float | None = None
    # the confirmed Compose service definition the runtime launches
    image: str = ""
    command: str | list | None = None
    entrypoint: str | list | None = None
    environment: Mapping | list = field(default_factory=dict)
    healthcheck: Mapping | None = None
    depends_on: Mapping | list = field(default_factory=list)
    user: str | None = None
    working_dir: str | None = None
    restart: str | None = None


@dataclass(frozen=True)
class ServicePlan:
    ref: str
    checkout: str
    group: str
    memory_bytes: int
    cpus: float
    networks: tuple[str, ...]
    volumes: tuple[str, ...]
    containers: tuple[ContainerSpec, ...]
    checkouts: Mapping[str, str] = field(default_factory=dict)   # repo -> its checkout


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str = ""


@dataclass(frozen=True)
class Blocker:
    kind: str       # missing-service-definition | incompatible-compose | resource-allowance
    service: str
    detail: str


class SandboxBlocked(Exception):
    def __init__(self, blockers):
        self.blockers = tuple(blockers)
        super().__init__("; ".join(f"{b.kind}: {b.service}: {b.detail}" for b in self.blockers))


class EngineError(Exception):
    """The box runtime could not start a container."""


@dataclass(frozen=True)
class Executed:
    role: str       # "agent" | "service"
    container: str
    command: str
    exit_code: int
    repo: str = ""  # the repository whose checkout the command ran in


@dataclass(frozen=True)
class Failure:
    command: str
    service: str    # failing Compose service, "" when the agent's command failed
    exit_code: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class StartResult:
    ready: bool
    commands: tuple[Executed, ...] = ()
    failure: Failure | None = None


class Engine(Protocol):
    def create(self, plan: ServicePlan) -> None: ...
    def start(self, plan: ServicePlan, name: str) -> None: ...
    # `workdir` is passed only for a repository other than the origin's, whose
    # checkout is the default /workspace.
    def exec(self, name: str, command: str, workdir: str = WORKSPACE) -> ExecResult: ...
    def stop(self, plan: ServicePlan, name: str) -> None: ...
    def remove(self, plan: ServicePlan) -> None: ...


_UNITS = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}


def _parse_memory(value) -> int:
    text = str(value).strip().lower().removesuffix("b")
    if text and text[-1] in _UNITS:
        return int(float(text[:-1]) * _UNITS[text[-1]])
    return int(text)


def _declared_limits(definition: dict) -> tuple[int | None, float | None]:
    limits = ((definition.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    memory, cpus = limits.get("memory"), limits.get("cpus")
    return _optional_limit(memory, _parse_memory), _optional_limit(cpus, float)


def _finite_positive(value):
    return math.isfinite(value) and value > 0


def _optional_limit(value, parse):
    if value is None:
        return None
    parsed = parse(value)
    if not _finite_positive(parsed):
        raise ValueError("memory and cpu limits must be finite and positive")
    return parsed


def _remaining_share(capped, total, uncapped):
    if not all(_finite_positive(v) for v in [total, *capped]):
        return None
    remaining = total - sum(capped)
    if remaining < 0 or (uncapped and remaining == 0):
        return None
    return remaining / uncapped if uncapped else 0


def _allocate(declared: dict[str, float | None], total: float) -> dict[str, float] | None:
    """Preserve declared caps; divide the remainder among uncapped containers."""
    capped = [v for v in declared.values() if v is not None]
    share = _remaining_share(capped, total, len(declared) - len(capped))
    if share is None:
        return None
    return {k: v if v is not None else share for k, v in declared.items()}


def _volume_parts(volume) -> tuple[str, str, str]:
    """(source, destination, mode) of a short- or long-form Compose volume; an
    anonymous volume has an empty source."""
    if isinstance(volume, dict):
        return (str(volume.get("source", "")), str(volume.get("target", "")),
                "ro" if volume.get("read_only") else "rw")
    parts = str(volume).split(":")
    if len(parts) == 1:
        return "", parts[0], "rw"
    return parts[0], parts[1], parts[2] if len(parts) > 2 else "rw"


# Compose keys that cannot widen access; any other key is a blocker (allowlist).
_SAFE_KEYS = {"image", "command", "entrypoint", "environment", "healthcheck", "deploy",
              "volumes", "expose", "working_dir", "user", "restart", "labels", "depends_on"}
# Of those, what ContainerSpec carries (volumes and limits become mounts and allocation).
_RUNTIME_KEYS = {"image", "command", "entrypoint", "environment", "healthcheck", "depends_on",
                 "user", "working_dir", "restart"}


def _volume_form_problem(volume) -> str:
    if isinstance(volume, dict):
        if volume.get("type", "volume") != "volume":
            return f"volume {volume!r} requires an unsupported mount type"
        if set(volume) - {"type", "source", "target", "read_only"}:
            return f"volume {volume!r} requires unsupported mount settings"
    return ""


def _volume_problem(volume, volumes: dict) -> str:
    if problem := _volume_form_problem(volume):
        return problem
    source, dest, mode = _volume_parts(volume)
    if not source or not dest:
        return f"volume {volume!r} is anonymous or has no mount path"
    if mode not in ("ro", "rw"):
        return f"volume {volume!r} uses mount option {mode!r}, which is not supported"
    if source not in volumes:
        return f"volume source {source!r} is not owned by this task's sandbox"
    if not isinstance(volumes[source], dict):
        return f"volume {source!r} must have a mapping definition"
    if extra := set(volumes[source]) - {"labels"}:
        return (f"volume {source!r} sets {sorted(extra)}: external, named or "
                f"host-backed volumes are not granted")
    return ""


def _validate_deploy(deploy):
    """Only resource limits can be represented by the runtime contract."""
    resources = deploy.get("resources") or {}
    limits = resources.get("limits") or {}
    if (set(deploy) - {"resources"} or set(resources) - {"limits"}
            or set(limits) - {"memory", "cpus"}):
        raise ValueError("unsupported deploy requirement")


def _service_resources_problem(definition):
    try:
        _validate_deploy(definition.get("deploy") or {})
        if not isinstance(definition.get("volumes", []), list):
            return "volumes must be a list"
        _declared_limits(definition)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return "declares unsupported deploy settings or invalid memory/cpu limits"
    return ""


def _service_problem(definition: dict, volumes: dict) -> str:
    """First isolation-breaking or unusable requirement of a Compose service, or ''."""
    for key in definition:
        if key not in _SAFE_KEYS:
            return f"{key!r} is not supported in a task sandbox and is not granted"
    if not isinstance(definition.get("image"), str) or not definition["image"]:
        return "has no image to run"
    if problem := _service_resources_problem(definition):
        return problem
    return next(filter(None, (_volume_problem(v, volumes)
                              for v in definition.get("volumes") or [])), "")


def _compose_doc(path: Path) -> tuple[dict, dict]:
    """(services, volumes) of a Compose file; raises on anything but mappings."""
    doc = yaml.safe_load(path.read_text()) or {}
    volumes = dict(doc.get("volumes") or {})
    return dict(doc.get("services") or {}), {k: {} if v is None else v for k, v in volumes.items()}


def _units(req: SandboxRequest) -> tuple[SandboxMember, ...]:
    """The origin repository and the members, as uniform checkout units."""
    return (SandboxMember(req.repo, req.clone, req.services, req.setup, req.gate,
                          req.compose, req.branch), *req.members)


def _service_mounts(definition: dict, owned: dict, unit: int) -> list[Mount]:
    parts = (_volume_parts(v) for v in definition.get("volumes") or [])
    return [Mount(owned[unit, source], dest, mode) for source, dest, mode in parts]


def _commands(req: SandboxRequest) -> list[tuple[str, str]]:
    """(repo, command) in run order: every setup, then every gate when it runs."""
    phases = ("setup", "gate") if req.run_gate else ("setup",)
    return [(unit.repo, command) for phase in phases for unit in _units(req)
            if (command := getattr(unit, phase))]


def _all_defs(composes) -> dict:
    return {name: defs[name] for unit, defs, _, _ in composes for name in unit.services}


def _workdir(req: SandboxRequest, repo: str) -> str:
    """Where the agent container mounts `repo`'s checkout."""
    return WORKSPACE if repo == req.repo else f"/workspaces/{repo.replace('/', '~')}"


def _digest(ref: str) -> str:
    """Collision-free resource prefix for an execution ref (names built by
    joining ref and volume with a separator would be ambiguous)."""
    return "t" + hashlib.sha256(ref.encode()).hexdigest()[:16]


def _checkout_label(project: str, ref: str) -> str:
    """`<project>-task-<issue>`, one path component, so an operator sees whose
    checkout it is; the digest after it keeps the name unique."""
    issue = ref.rpartition("#")[2]
    name = re.sub(r"[^A-Za-z0-9_-]", "_", project)
    return f"{name}-task-{issue}" if issue.isdigit() else name


def _plan_from_json(data: dict) -> ServicePlan:
    containers = tuple(ContainerSpec(**{**c, "mounts": tuple(Mount(**m) for m in c["mounts"])})
                       for c in data["containers"])
    return ServicePlan(**{**data, "networks": tuple(data["networks"]),
                          "volumes": tuple(data["volumes"]), "containers": containers})


def _serialized(method):
    @wraps(method)
    def operation(self, execution_ref):
        with self._locked(execution_ref):
            return method(self, execution_ref)
    return operation


class TaskSandbox:
    def __init__(self, engine: Engine, state_dir: str | Path,
                 requests: Mapping[str, SandboxRequest]):
        self.engine, self.state_dir, self.requests = engine, Path(state_dir), requests
        self.plans: dict[str, ServicePlan] = {}

    @_serialized
    def prepare(self, execution_ref: str) -> ServicePlan:
        req = self.requests[execution_ref]
        if self._plan_path(execution_ref).exists():
            plan = self._plan_for(execution_ref)
            self._checkouts(req, plan)
            self.engine.create(plan)
            return plan
        composes = [(unit, *self._read_compose(unit)) for unit in _units(req)]
        allocation = self._allocated(req, composes)
        plan = self._plan(execution_ref, req, composes, allocation)
        self._save(plan)    # ownership is durable before anything is allocated
        self._checkouts(req, plan)
        self.engine.create(plan)
        return plan

    @_serialized
    def establish_scope(self, execution_ref: str) -> ServicePlan:
        """Controller-only initial exploration handover, after ending its session.

        Reallocate the same sandbox for the initial member set, keeping checkout
        contents and service volumes. Ordinary prepare/resume never widen scope.
        """
        req = self.requests[execution_ref]
        old = self._plan_for(execution_ref)
        if set(old.checkouts) == {unit.repo for unit in _units(req)}:
            self._checkouts(req, old)
            self.engine.create(old)
            return old
        composes = [(unit, *self._read_compose(unit)) for unit in _units(req)]
        plan = self._plan(execution_ref, req, composes, self._allocated(req, composes))
        self._require_initial_scope(old, plan, req.repo)
        for container in old.containers:
            self.engine.stop(old, container.name)
        # Remove only containers and the resource group, preserving owned data
        # and network. The durable old plan still owns these if removal fails.
        self.engine.remove(replace(old, volumes=(), networks=()))
        self._save(plan)
        self._checkouts(req, plan)
        self.engine.create(plan)
        return plan

    @staticmethod
    def _require_initial_scope(old: ServicePlan, plan: ServicePlan, origin: str) -> None:
        if set(old.checkouts) != {origin}:
            raise EngineError("only an origin-only exploration can establish scope")
        if old.checkout != plan.checkout or not set(old.volumes) <= set(plan.volumes):
            raise EngineError("initial scope must preserve existing checkout and service data")

    def _allocated(self, req, composes):
        """The combined allocation, or SandboxBlocked with every reason it cannot be."""
        blockers = [b for _, _, _, unreadable in composes for b in unreadable]
        blockers = blockers or self._compose_blockers(composes)
        allocation = None if blockers else self._allocation(req, _all_defs(composes))
        if allocation is None and not blockers:
            blockers = [Blocker("resource-allowance", "",
                                f"declared memory/cpu exceed the combined {req.limits}")]
        if blockers:
            raise SandboxBlocked(blockers)
        return allocation

    @staticmethod
    def _read_compose(unit):
        """(services, volumes, blockers): Compose is read only when services are
        confirmed; a missing or malformed file blocks each of them."""
        if not unit.services:
            return {}, {}, []
        try:
            return (*_compose_doc(Path(unit.clone) / unit.compose), [])
        except (OSError, AttributeError, TypeError, ValueError, yaml.YAMLError) as error:
            return {}, {}, [Blocker("missing-service-definition", name,
                                    f"{unit.compose} is missing or unusable ({error})")
                            for name in unit.services]

    @contextmanager
    def _locked(self, ref):
        directory = self.state_dir / "locks"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / _digest(ref)).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Another dispatcher may have changed or removed the durable record.
            self.plans.pop(ref, None)
            yield

    def _plan_path(self, ref: str) -> Path:
        return self.state_dir / "plans" / f"{_digest(ref)}.json"

    def _save(self, plan: ServicePlan) -> None:
        self.plans[plan.ref] = plan
        path = self._plan_path(plan.ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(asdict(plan), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _plan_for(self, ref: str) -> ServicePlan:
        """The plan recorded at prepare, recovered from disk after a restart."""
        if ref not in self.plans:
            self.plans[ref] = _plan_from_json(json.loads(self._plan_path(ref).read_text()))
        return self.plans[ref]

    @staticmethod
    def _compose_blockers(composes) -> list[Blocker]:
        found, seen = [], set()
        for unit, defs, volumes, _ in composes:
            for name in unit.services:
                if name in seen:
                    found.append(Blocker("incompatible-compose", name,
                                         "another repository of this task defines the same service"))
                elif not isinstance(defs.get(name), dict) or not defs[name]:
                    found.append(Blocker("missing-service-definition", name,
                                         f"{unit.compose} has no usable definition for {name}"))
                elif problem := _service_problem(defs[name], volumes):
                    found.append(Blocker("incompatible-compose", name, problem))
                seen.add(name)
        return found

    @staticmethod
    def _allocation(req, defs) -> dict[str, tuple[float, float]] | None:
        caps = {name: _declared_limits(defs[name]) for u in _units(req) for name in u.services}
        demand = req.agent_demand
        caps[""] = (demand.memory_bytes, demand.cpus) if demand else (None, None)
        memory = _allocate({k: v[0] for k, v in caps.items()}, req.limits.memory_bytes)
        cpus = _allocate({k: v[1] for k, v in caps.items()}, req.limits.cpus)
        if memory is None or cpus is None:
            return None
        return {k: (int(memory[k]), cpus[k]) for k in caps}

    def _plan(self, ref, req, composes, allocation) -> ServicePlan:
        prefix = _digest(ref)
        checkout = str(self.state_dir / "checkouts" / f"{_checkout_label(req.project, ref)}-{prefix}")
        checkouts = {req.repo: checkout, **{
            m.repo: f"{checkout}-{i}" for i, m in enumerate(req.members, 1)}}
        group, network = f"{prefix}-group", f"{prefix}-net"
        defs = _all_defs(composes)
        # a member's volumes are namespaced: two repositories may both declare `pgdata`;
        # `-m<i>-vol-` can never equal the origin's `-vol-<name>`
        owned = {(i, v): f"{prefix}-vol-{v}" if i == 0 else f"{prefix}-m{i}-vol-{v}"
                 for i, (_, _, volumes, _) in enumerate(composes) for v in sorted(volumes)}

        def spec(service, mounts):
            memory, cpus = allocation[service]
            config = {k: v for k, v in (defs[service] if service else {}).items()
                      if k in _RUNTIME_KEYS}
            name = f"{prefix}-svc-{service}" if service else f"{prefix}-agent"
            return ContainerSpec(name, "service" if service else "agent",
                                 service, tuple(mounts), network, group,
                                 memory_bytes=memory, cpus=cpus, **config)

        containers = [spec("", [Mount(path, _workdir(req, repo)) for repo, path in checkouts.items()])]
        containers += [spec(name, _service_mounts(unit_defs[name], owned, i))
                       for i, (unit, unit_defs, _, _) in enumerate(composes) for name in unit.services]
        return ServicePlan(ref, checkout, group, req.limits.memory_bytes, req.limits.cpus,
                           (network,), tuple(owned.values()), tuple(containers), checkouts)

    def _checkouts(self, req: SandboxRequest, plan: ServicePlan) -> None:
        for unit in _units(req):
            self._checkout(unit, plan.checkouts.get(unit.repo, plan.checkout))

    @staticmethod
    def _checkout(req, checkout: str) -> None:
        # Once exposed to a task, even .git/config and hooks are untrusted.
        # Never run host Git against a retained checkout on retry.
        if not (Path(checkout) / ".git").exists():
            Path(checkout).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--no-hardlinks", req.clone, checkout],
                           check=True, capture_output=True)
            # the shared clone's origin is a host path the agent cannot reach; keep its remote
            remote = subprocess.run(["git", "-C", req.clone, "remote", "get-url", "origin"],
                                    capture_output=True, text=True)
            if remote.returncode == 0:
                subprocess.run(["git", "-C", checkout, "remote", "set-url", "origin",
                                remote.stdout.strip()], check=True, capture_output=True)
                subprocess.run(["git", "-C", checkout, "fetch", "origin", "main"],
                               check=True, capture_output=True)
            base = "FETCH_HEAD" if remote.returncode == 0 else "HEAD"
            branch = ["-b", req.branch] if req.branch else ["--detach"]
            subprocess.run(["git", "-C", checkout, "checkout", *branch, base],
                           check=True, capture_output=True)

    def _start_containers(self, plan: ServicePlan) -> Failure | None:
        started = []
        for c in sorted(plan.containers, key=lambda c: c.role != "service"):
            try:
                self.engine.start(plan, c.name)
            except EngineError as error:
                for name in started:
                    self.engine.stop(plan, name)
                return Failure("start", c.service, detail=str(error))
            started.append(c.name)
        return None

    @_serialized
    def start(self, execution_ref: str) -> StartResult:
        plan, req = self._plan_for(execution_ref), self.requests[execution_ref]
        if failure := self._start_containers(plan):
            return StartResult(False, failure=failure)
        return self._run_checks(plan, req)

    def _run_checks(self, plan: ServicePlan, req: SandboxRequest) -> StartResult:
        """Every repository's setup, then every repository's gate, each in its checkout."""
        agent = next(c.name for c in plan.containers if c.role == "agent")
        executed = []
        for repo, command in _commands(req):
            result = self._exec(agent, command, _workdir(req, repo))
            executed.append(Executed("agent", agent, command, result.exit_code, repo))
            if result.exit_code:
                return StartResult(False, tuple(executed),
                                   Failure(command, "", result.exit_code, result.output))
        return StartResult(True, tuple(executed))

    def _exec(self, agent: str, command: str, workdir: str) -> ExecResult:
        if workdir == WORKSPACE:
            return self.engine.exec(agent, command)
        return self.engine.exec(agent, command, workdir=workdir)

    @_serialized
    def park(self, execution_ref: str) -> None:
        """Stop every container; one that will not stop is reported after the
        others were tried."""
        plan = self._plan_for(execution_ref)
        failed = None
        for c in plan.containers:
            try:
                self.engine.stop(plan, c.name)
            except EngineError as error:
                failed = failed or error
        if failed:
            raise failed

    def owns(self, execution_ref: str) -> bool:
        return self._plan_path(execution_ref).exists()

    @_serialized
    def resume(self, execution_ref: str) -> StartResult:
        failure = self._start_containers(self._plan_for(execution_ref))
        return StartResult(failure is None, failure=failure)

    @_serialized
    def cleanup(self, execution_ref: str) -> None:
        try:
            plan = self._plan_for(execution_ref)
        except FileNotFoundError:
            return      # already cleaned up
        self.engine.remove(plan)    # a failure keeps the plan so cleanup can be retried
        for checkout in dict.fromkeys((plan.checkout, *plan.checkouts.values())):
            if Path(checkout).exists():
                shutil.rmtree(checkout)    # a failure keeps the plan so cleanup can be retried
        self._plan_path(execution_ref).unlink(missing_ok=True)
        self.plans.pop(execution_ref, None)
