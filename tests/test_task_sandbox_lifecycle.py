"""Park / resume / cleanup and long-form volumes of the task sandbox (ticket 09)."""
import subprocess
from pathlib import Path

import yaml

from dispatcher.sandbox import EngineError, ExecResult, SandboxRequest, TaskSandbox


class Engine:
    def __init__(self):
        self.running, self.removed, self.fail = set(), [], set()

    def create(self, plan): pass

    def start(self, plan, name):
        if name in self.fail:
            raise EngineError(name)
        self.running.add(name)

    def stop(self, plan, name): self.running.discard(name)

    def remove(self, plan): self.removed.append(plan.ref)

    def exec(self, name, command): return ExecResult(0)


def sandbox(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    doc = {"services": {"db": {"image": "x", "volumes": [{"type": "volume", "source": "d", "target": "/data"}]}},
           "volumes": {"d": {}}}
    (clone / "compose.yaml").write_text(yaml.safe_dump(doc))
    (clone / "f").write_text("f")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *cmd], cwd=clone, check=True, capture_output=True)
    req = SandboxRequest("p", "me/p", str(clone), services=("db",), setup="", gate="", run_gate=False)
    engine = Engine()
    return engine, TaskSandbox(engine, tmp_path / "state", {"t1": req})


def test_park_resume_cleanup(tmp_path):
    engine, box = sandbox(tmp_path)
    plan = box.prepare("t1")
    assert plan.containers[1].mounts[0].dest == "/data"
    assert box.start("t1").ready and len(engine.running) == 2

    box.park("t1")
    assert engine.running == set()
    assert box.resume("t1").ready and len(engine.running) == 2

    box.cleanup("t1")
    assert engine.removed == ["t1"] and not Path(plan.checkout).exists()


def test_resume_reports_service_that_cannot_start(tmp_path):
    engine, box = sandbox(tmp_path)
    plan = box.prepare("t1")
    engine.fail.add(next(c.name for c in plan.containers if c.service == "db"))
    result = box.resume("t1")
    assert not result.ready and result.failure.service == "db"


def test_unlisted_compose_key_is_blocked(tmp_path):
    import pytest
    from dispatcher.sandbox import SandboxBlocked
    _, box = sandbox(tmp_path)
    doc = yaml.safe_load((tmp_path / "clone/compose.yaml").read_text())
    doc["services"]["db"]["cap_add"] = ["SYS_ADMIN"]
    (tmp_path / "clone/compose.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(SandboxBlocked) as blocked:
        box.prepare("t1")
    assert blocked.value.blockers[0].kind == "incompatible-compose"


def test_colliding_looking_refs_get_distinct_volumes_and_cleanup_retries(tmp_path):
    engine, box = sandbox(tmp_path)
    box.requests["t1-d"] = box.requests["t1"]
    a, b = box.prepare("t1"), box.prepare("t1-d")
    assert not set(a.volumes) & set(b.volumes)
    assert not {c.name for c in a.containers} & {c.name for c in b.containers}

    restarted = TaskSandbox(engine, tmp_path / "state", box.requests)   # dispatcher restart
    restarted.park("t1")
    engine.remove = lambda plan: (_ for _ in ()).throw(RuntimeError("busy"))
    try:
        restarted.cleanup("t1")
    except RuntimeError:
        pass
    engine.remove = lambda plan: engine.removed.append(plan.ref)
    restarted.cleanup("t1")
    restarted.cleanup("t1")
    assert engine.removed == ["t1"]


def test_malformed_compose_blocks_the_selected_service(tmp_path):
    import pytest
    from dispatcher.sandbox import SandboxBlocked
    _, box = sandbox(tmp_path)
    (tmp_path / "clone/compose.yaml").write_text("- just\n- a list\n")
    with pytest.raises(SandboxBlocked) as blocked:
        box.prepare("t1")
    assert [b.service for b in blocked.value.blockers] == ["db"]
