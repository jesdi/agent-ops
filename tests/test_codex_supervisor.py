"""Supervisor regressions through main(argv), real listener and external Codex."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import asyncio
import json
import os
from pathlib import Path
import signal
import sys

import pytest
from websockets.asyncio.client import unix_connect

from tests.runtime_listener import launch_listener  # noqa: F401
from dispatcher.codex_supervisor import main
from dispatcher.runtime_http import RuntimeClient
from dispatcher.state import read_session
from tests.test_bound_turns_t3_acceptance import (  # noqa: F401
    isolated, eventually, RunningSupervisor, PROVIDER, PROMPT,
)


@contextmanager
def running_main(isolated, monkeypatch, *, mode="compatible", resume=None, provider=PROVIDER):
    root, state, worktree = isolated
    fixture = root / "provider"
    fixture.mkdir()
    (fixture / "mode").write_text(mode)
    binary_dir = root / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "codex"
    binary.write_text(f"#!{sys.executable}\n__file__ = {str(provider)!r}\nexec(compile(open({str(provider)!r}).read(), {str(provider)!r}, 'exec'))\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("T3_PROVIDER_FIXTURE", str(fixture))
    prompt = worktree / ".agent" / "prompt-review.md"
    prompt.write_text(PROMPT)
    binding = RuntimeClient(state).prepare("project-a", 370, "review", runtime="codex",
                            conversation_id=resume, worktree=str(worktree))["binding"]
    for key, value in binding.items():
        monkeypatch.setenv(f"AGENT_OPS_{key.upper()}", str(value or ""))
    args = ["--model", "gpt-6.1-sol", "--prompt-file", str(prompt), "--effort", "high"]
    if resume is not None:
        args.append(f"--resume={resume}")
    with (fixture / "supervisor.log").open("w+") as output, ThreadPoolExecutor() as executor:
        future = executor.submit(main, args)
        running = RunningSupervisor(fixture, state, worktree, binding, None, output)
        try:
            yield running, future
        finally:
            for record in running.records("backend-started"):
                try:
                    os.kill(record["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
            future.result(timeout=10)


def test_control_completion_records_bound_stage_without_task_state(isolated, monkeypatch):
    with running_main(isolated, monkeypatch) as (running, future):
        identity = running.ready()
        running.command("stop", threadId=identity)
        eventually(lambda: running.snapshot()["main"]["status"] == "stopped",
                   "normal current control completion must end the main turn")
        record = eventually(lambda: read_session(running.state, "project-a", 370), "completion must record session")
        assert record.session_id == identity
        assert record.stage == "review"
        assert not future.done()


def test_accepted_initial_turn_attaches_terminal_despite_lost_ack(isolated, monkeypatch):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, mode="bootstrap-lost-ack", provider=provider) as (running, future):
        eventually(lambda: running.records("bootstrap-ack-dropped"), "provider must accept prompt and drop its reply")
        identity = running.ready()
        running.command("output", threadId=identity, text="Still connected after lost reply")
        eventually(lambda: running.records("terminal-output"), "attached terminal must display ongoing output")
        starts = [r for r in running.records("rpc") if r["method"] == "turn/start"]
        assert len(starts) == 1
        assert starts[0]["params"]["input"] == [{"type": "text", "text": PROMPT}]
        assert starts[0]["params"]["threadId"] == identity
        assert len(running.records("backend-started")) == 1
        assert len(running.records("terminal-started")) == 1
        assert running.snapshot()["main"]["status"] == "active"
        assert running.snapshot()["service"] == "live"
        assert not future.done()


@pytest.mark.parametrize("status", ["active", "completed", "queued-completion", "delayed",
                                    "failed", "failed-system-error", "interrupted", "later-turn"])
def test_recovered_initial_input_attaches_without_submitting_again(isolated, monkeypatch, status):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, mode=f"bootstrap-recover-{status}",
                      provider=provider, resume="existing-root") as (running, future):
        eventually(lambda: running.records("bootstrap-start-dropped"), "initial reply and event must be lost")
        identity = running.ready()
        assert identity == "existing-root"
        starts = [r for r in running.records("rpc") if r["method"] == "turn/start"]
        assert len(starts) == 1
        assert starts[0]["params"]["input"] == [{"type": "text", "text": PROMPT}]
        assert not [r for r in running.records("rpc") if r["method"] == "thread/start"]
        assert len(running.records("backend-started")) == len(running.records("terminal-started")) == 1
        running.command("output", threadId=identity, text="Same root after recovery")
        eventually(lambda: running.records("terminal-output"), "recovered terminal must remain usable")
        assert running.snapshot()["service"] == "live"
        if status == "queued-completion":
            record = eventually(lambda: read_session(running.state, "project-a", 370),
                                "ordered recovery must retain subsequent normal completion")
            assert record.session_id == identity
            assert record.stage == "review"
            assert running.snapshot()["main"]["status"] == "stopped"
        else:
            assert read_session(running.state, "project-a", 370) is None
        if status in {"failed", "failed-system-error", "interrupted"}:
            assert running.snapshot()["main"]["status"] == "unknown"
        if status == "later-turn":
            assert running.snapshot()["main"]["turn_id"] == "later-native-turn"
        assert not future.done()


@pytest.mark.parametrize("variant", ["foreign", "turns-shape", "active-empty", "idle-empty",
    "turn-shape", "turn-id-type", "turn-id-empty", "inconsistent-status", "items-shape", "old-input", "invalid-status",
    "historical-invalid-turn"])
def test_unverified_recovery_does_not_attach_terminal(isolated, monkeypatch, variant):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, mode=f"bootstrap-recover-{variant}",
                      provider=provider, resume="existing-root") as (running, future):
        eventually(lambda: running.records("bootstrap-recovery-response"), "faulty recovery response must be sent")
        if variant in {"idle-empty", "old-input"}:
            eventually(lambda: len([r for r in running.records("rpc")
                                   if r["method"] == "thread/backgroundTerminals/list"]) >= 2,
                       "controller must finish inspecting uncorrelated history")
        else:
            eventually(lambda: len(running.records("bootstrap-recovery-response")) >= 2,
                       "controller must reject and retry incompatible recovery")
        assert not running.records("terminal-started")
        assert len([r for r in running.records("rpc") if r["method"] == "turn/start"]) == 1
        assert running.snapshot()["binding"]["conversation_id"] == "existing-root"
        assert read_session(running.state, "project-a", 370) is None
        assert not future.done()


def test_disconnect_keeps_terminal_and_does_not_resubmit_initial_prompt(isolated, monkeypatch):
    with running_main(isolated, monkeypatch, resume="--last") as (running, future):
        identity = running.ready()
        eventually(lambda: any(r["method"] == "thread/backgroundTerminals/list"
                               for r in running.records("rpc")), "inventory must be read")
        running.command("disconnect-control")
        dropped = eventually(lambda: running.records("control-disconnected"), "connection must drop")[-1]
        eventually(lambda: any(r["connection"] > max(dropped["connections"])
                               and r["method"] in ("thread/read", "thread/resume") for r in running.records("rpc")),
                   "new connection must recover the exact thread")
        running.command("output", threadId=identity, text="Recovered terminal output")
        eventually(lambda: running.records("terminal-output"), "original terminal must display output")
        assert len([r for r in running.records("rpc") if r["method"] == "turn/start"]) == 1
        assert running.snapshot()["binding"]["conversation_id"] == "--last"
        assert not future.done()


def test_incompatible_inventory_holds_launch_with_one_problem(isolated, monkeypatch):
    with running_main(isolated, monkeypatch, mode="malformed") as (running, future):
        running.ready()
        eventually(lambda: running.snapshot()["alerts"], "unreadable inventory must be reported")
        assert running.snapshot()["inventory"] == "unknown"
        assert not future.done()
        eventually(lambda: len([r for r in running.records("rpc")
                               if r["method"] == "thread/backgroundTerminals/list"]) >= 2,
                   "compatibility probe must retry")
        assert len(running.snapshot()["alerts"]) == 1


def test_unsupported_inventory_preserves_live_terminal(isolated, monkeypatch):
    with running_main(isolated, monkeypatch, mode="unsupported") as (running, future):
        running.ready()
        eventually(lambda: running.snapshot()["alerts"], "unsupported inventory must be reported")
        assert running.snapshot()["main"]["status"] == "unknown"
        assert not future.done()


def test_service_death_ends_supervisor_when_terminal_does_not_exit(isolated, monkeypatch):
    with running_main(isolated, monkeypatch, mode="terminal-survives") as (running, future):
        running.ready()
        os.kill(running.records("backend-started")[0]["pid"], signal.SIGTERM)
        assert future.result(timeout=5) == 1
        assert running.snapshot()["service"] == "dead"


def test_operator_terminal_exit_ends_backend_and_session(isolated, monkeypatch):
    with running_main(isolated, monkeypatch) as (running, future):
        running.ready()
        os.kill(running.records("terminal-started")[0]["pid"], signal.SIGTERM)
        assert future.result(timeout=5) != 0
        assert running.snapshot()["service"] == "dead"


def test_invalid_inventory_row_reports_compatibility_problem(isolated, monkeypatch):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, mode="invalid-row", provider=provider) as (running, future):
        running.ready()
        eventually(lambda: running.snapshot()["alerts"], "invalid inventory entries must be reported")
        assert running.snapshot()["inventory"] == "unknown"
        assert not future.done()


@pytest.mark.parametrize("params", [None, {"threadId": "ROOT", "turn": []},
    {"threadId": "ROOT", "turn": {"id": None, "status": "completed"}},
    {"threadId": "ROOT", "turn": {"id": "", "status": "completed"}},
    {"threadId": "ROOT", "turn": {"id": "CURRENT", "status": "failed"}}])
def test_invalid_control_completion_cannot_record_a_stopped_turn(isolated, monkeypatch, params):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, provider=provider) as (running, future):
        identity = running.ready()
        eventually(lambda: running.snapshot()["main"]["status"] == "active", "main must be active")
        wire_params = json.loads(json.dumps(params).replace('ROOT', identity).replace(
            'CURRENT', running.snapshot()["main"]["turn_id"]))
        (running.root / "wire.jsonl").write_text(json.dumps({"method": "turn/completed", "params": wire_params}) + "\n")
        eventually(lambda: running.snapshot()["alerts"], "malformed lifecycle must be reported")
        assert running.snapshot()["main"]["status"] == "unknown"
        assert read_session(running.state, "project-a", 370) is None
        assert not future.done()


def test_foreign_control_completion_does_not_end_bound_main(isolated, monkeypatch):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, provider=provider) as (running, future):
        running.ready()
        before = running.snapshot()["main"]
        packets = [
            {"method": "turn/completed", "params": {"threadId": "foreign-title", "turn": {"id": before["turn_id"], "status": "completed"}}},
            {"method": "turn/completed", "params": None},
        ]
        # The following malformed packet supplies an observable barrier after
        # the foreign notification on the same ordered controller connection.
        (running.root / "wire.jsonl").write_text("\n".join(map(json.dumps, packets)) + "\n")
        eventually(lambda: running.snapshot()["alerts"], "barrier packet must reach controller")
        assert read_session(running.state, "project-a", 370) is None
        assert not future.done()


def test_gateway_forwards_server_requests_and_correlated_operator_answers(isolated, monkeypatch):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, provider=provider) as (running, future):
        identity = running.ready()
        endpoint = running.records("terminal-started")[0]["endpoint"]

        async def approve():
            async with unix_connect(path=endpoint.removeprefix("unix://")) as socket:
                await socket.send(json.dumps({"id": "operator-init", "method": "initialize", "params": {"clientInfo": {"name": "operator", "version": "1"}}}))
                assert json.loads(await socket.recv())["id"] == "operator-init"
                request = {"id": "approval-42", "method": "item/commandExecution/requestApproval",
                           "params": {"threadId": identity, "turnId": "turn-1", "itemId": "command-1"}}
                (running.root / "wire.jsonl").write_text(json.dumps(request) + "\n")
                packet = json.loads(await asyncio.wait_for(socket.recv(), 2))
                assert packet == request
                await socket.send(json.dumps({"id": packet["id"], "result": {"decision": "accept"}}))
                # A correlated round-trip guarantees the reply was forwarded.
                await socket.send(json.dumps({"id": "after-answer", "method": "thread/read", "params": {"threadId": identity}}))
                assert json.loads(await socket.recv())["id"] == "after-answer"

        asyncio.run(approve())
        replies = running.records("client-response")
        assert [r["packet"] for r in replies] == [{"id": "approval-42", "result": {"decision": "accept"}}]
        assert not future.done()


@pytest.mark.parametrize("message", [None, "", 1, [], {}])
def test_runtime_rejects_invalid_compatibility_problem_without_changing_state(tmp_path, message):
    from dispatcher.runtime_control import RuntimeControl
    control = RuntimeControl(tmp_path)
    initial = control.prepare("project-a", 370, "review", conversation_id="root")
    assert not control.event(initial["binding"], {"type": "control/unknown", "message": message})
    assert control.view("project-a", 370) == initial


def test_runtime_compatibility_problem_survives_restart_once_and_cannot_cross_launch(tmp_path):
    from dispatcher.runtime_control import RuntimeControl
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project-a", 370, "review", conversation_id="root")["binding"]
    assert control.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "turn-1"})
    assert control.event(binding, {"type": "turn/completed", "thread_id": "root", "turn_id": "turn-1", "status": "completed"})
    problem = {"type": "control/unknown", "message": "Unsupported inventory payload"}
    assert control.event(binding, problem)
    assert control.event(binding, problem)
    restored = RuntimeControl(tmp_path).view("project-a", 370)
    assert restored["main"]["status"] == "unknown"
    assert restored["inventory"] == "unknown"
    assert restored["alerts"] == [{"kind": "compatibility", "message": "Unsupported inventory payload"}]
    replacement = control.prepare("project-a", 370, "review", conversation_id="replacement")
    assert not control.event(binding, problem)
    assert control.view("project-a", 370) == replacement


def test_reconnect_reconciles_active_turn_before_subsequent_completion(isolated, monkeypatch):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, mode="completion-on-resume", provider=provider) as (running, future):
        identity = running.ready()
        eventually(lambda: any(r["method"] == "thread/backgroundTerminals/list" for r in running.records("rpc")),
                   "controller must observe inventory before disconnect")
        running.command("disconnect-control")
        eventually(lambda: running.snapshot()["main"]["status"] == "stopped",
                   "reconnected controller must subscribe and apply the subsequent completion")
        record = eventually(lambda: read_session(running.state, "project-a", 370), "completion must record session")
        assert record.session_id == identity and record.stage == "review"
        assert len([r for r in running.records("rpc") if r["method"] == "turn/start"]) == 1
        assert len(running.records("backend-started")) == len(running.records("terminal-started")) == 1
        assert not future.done()


@pytest.mark.usefixtures("launch_listener")
def test_http_completion_records_bound_stage_and_rejects_old_launch(tmp_path):
    from dispatcher.state import SessionRecord
    client = RuntimeClient(tmp_path)
    binding = client.prepare("project-a", 370, "implement", ticket="4", conversation_id="root")["binding"]
    assert client.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "turn-1"})
    completion = {"type": "turn/completed", "thread_id": "root", "turn_id": "turn-1", "status": "completed"}
    assert client.event(binding, completion)
    assert read_session(tmp_path, "project-a", 370) == SessionRecord("root", "implement")
    client.prepare("project-a", 370, "review", conversation_id="new-root")
    assert not client.event(binding, completion)
    assert read_session(tmp_path, "project-a", 370) == SessionRecord("root", "implement")


def test_recovery_rejects_old_turns_and_restores_latest_or_new_active_turn(tmp_path):
    from dispatcher.runtime_control import RuntimeControl
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project-a", 370, "review", conversation_id="root")["binding"]
    for turn in ("old", "current"):
        assert control.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": turn})
    assert control.event(binding, {"type": "control/unknown", "message": "Control reconnect"})
    recover = {"type": "turn/recovered", "thread_id": "root", "turn_id": "current", "status": "inProgress"}
    assert not control.event(binding, dict(recover, turn_id="old"))
    assert not control.event(binding, dict(recover, thread_id="foreign"))
    assert not control.event(binding, dict(recover, status="completed"))
    assert control.view("project-a", 370)["main"]["status"] == "unknown"
    assert control.event(binding, recover)
    assert not control.event(binding, recover)
    assert control.event(binding, {"type": "control/unknown", "message": "Control reconnect"})
    assert control.event(binding, dict(recover, turn_id="new-while-offline"))
    main = control.view("project-a", 370)["main"]
    assert main == {"status": "active", "turn_id": "new-while-offline", "seen_turns": ["old", "current", "new-while-offline"]}


def test_completed_output_item_does_not_end_or_invalidate_main_turn(isolated, monkeypatch):
    provider = Path(__file__).with_name("codex_control_fixture.py")
    with running_main(isolated, monkeypatch, provider=provider) as (running, future):
        identity = running.ready()
        current = eventually(lambda: running.snapshot()["main"].get("turn_id"), "main must start")
        packets = [
            {"method": "item/completed", "params": {"threadId": identity, "turnId": current,
                "item": {"type": "agentMessage", "id": "message-1", "text": "Output item completed"}}},
            {"method": "turn/started", "params": {"threadId": identity,
                "turn": {"id": "next-main-turn", "status": "inProgress", "items": []}}},
        ]
        (running.root / "wire.jsonl").write_text("\n".join(map(json.dumps, packets)) + "\n")
        # The ordered next-turn notification is a public processing barrier.
        eventually(lambda: running.snapshot()["main"].get("turn_id") == "next-main-turn",
                   "controller must process both notifications in order")
        assert running.snapshot()["main"]["status"] == "active"
        assert running.snapshot()["alerts"] == []
        assert read_session(running.state, "project-a", 370) is None
        assert not future.done()
