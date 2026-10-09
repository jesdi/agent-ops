"""Own the Codex service and attach its real terminal to one bound root."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed

from dispatcher.codex_transport import Gateway, ProtocolError, connect, admit_input, input_response
from dispatcher.runtime_http import BoundClient
from dispatcher.runtime_snapshots import valid_snapshot
from dispatcher.codex_inventory import NativeInventory
from dispatcher.codex_delivery import DeliveryPump


class Controller:
    def __init__(self, client, binding, arguments):
        self.client, self.binding, self.arguments = client, binding, arguments
        self.conversation = binding["conversation_id"]
        self.initial_attempted = False
        self.initial_submitted = False
        self.initial_input_id = f"agent-ops-bootstrap:{binding['launch_id']}"
        self.ready = asyncio.Event()
        self.work = NativeInventory()
        self.delivery = None

    async def event(self, **event):
        return await asyncio.to_thread(self.client.event, self.binding, event)

    async def problem(self, message):
        await self.event(type="control/unknown", message=message)

    async def notification(self, packet):
        try:
            changed = self.work.notification(packet, self.conversation)
        except (ProtocolError, KeyError, TypeError, ValueError) as exc:
            await self.problem(f"Codex worker compatibility: {exc}")
            return
        if changed:
            await self.event(type="inventory", certainty="unknown", workers=[])
        method = packet.get("method")
        if method not in ("turn/started", "turn/completed"):
            return
        try:
            turn = main_lifecycle_turn(packet.get("params"), method, self.conversation)
        except ProtocolError as exc:
            await self.problem(str(exc))
            return
        if turn is not None:
            await self.lifecycle(method, turn)

    async def lifecycle(self, method, turn):
        self.work.activity += 1
        await self.event(type="inventory", certainty="unknown", workers=[])
        accepted = await self.event(type=method, thread_id=self.conversation,
                                    turn_id=turn["id"], status=turn["status"])
        if accepted and self.initial_submitted and method == "turn/started":
            self.ready.set()

    async def bind(self, rpc, *, recover=False):
        params = dict(cwd=self.binding["worktree"], model=self.arguments.model,
                      approvalPolicy="never", sandbox="danger-full-access")
        if self.arguments.effort:
            params["config"] = {"model_reasoning_effort": self.arguments.effort}
        method = "thread/start"
        if self.conversation is not None:
            method = "thread/resume"
            params["threadId"] = self.conversation
        response = await rpc.call(method, params, on_result=self.recover if recover else None)
        identity = selected_conversation(response, self.conversation)
        if self.conversation is None:
            if not await self.event(type="bound", conversation_id=identity):
                raise ProtocolError("listener rejected conversation binding")
            snapshot = await asyncio.to_thread(self.client.view)
            self.binding = snapshot["binding"]
        self.conversation = identity

    async def start(self, rpc):
        # Attempt once: a lost reply must never cause a duplicate model turn.
        self.initial_attempted = True
        await self.bind(rpc)
        await self.event(type="service", status="live")
        await self.seed(rpc)
        params = {"threadId": self.conversation, "model": self.arguments.model,
                  "clientUserMessageId": self.initial_input_id,
                  "input": [{"type": "text", "text": self.arguments.prompt}]}
        if self.arguments.effort:
            params["effort"] = self.arguments.effort
        if not await admit_input(self.client, self.binding, self.initial_input_id):
            raise RuntimeError("listener rejected bootstrap admission")
        self.initial_submitted = True
        await rpc.call("turn/start", params, on_result=self.initial_ack)
        self.ready.set()

    async def initial_ack(self, result):
        await input_response(self.client, self.binding, self.initial_input_id, {"result": result})

    async def seed(self, rpc):
        checkpoint = dict(launch_id=self.binding["launch_id"], seeded=False,
                          baseline_turns=[], baseline_workers=[], seen_completions=[], scopes=[])
        await self.event(type="inventory", certainty="unknown", workers=[], history_checkpoint=checkpoint)
        await self.inventory(rpc, seed=True)

    async def inventory(self, rpc, *, seed=False):
        snapshot = await asyncio.to_thread(self.client.view)
        activity = self.work.activity
        try:
            event = await self.work.scan(rpc, snapshot, seed=seed)
            if activity != self.work.activity:
                event["certainty"] = "unknown"
            await self.event(**event)
        except (ProtocolError, KeyError, TypeError, ValueError) as exc:
            await self.event(type="inventory", certainty="unknown", workers=self.work.partial(snapshot),
                             message=f"Codex inventory compatibility: {exc}")
            checkpoint = snapshot.get("history_checkpoint")
            if not checkpoint or not checkpoint["seeded"]:
                await self.problem(f"Codex inventory compatibility: {exc}")

    async def recover(self, response):
        identity = selected_conversation(response, self.conversation)
        turn = recovered_turn(response["thread"])
        if turn is None:
            return
        if turn["status"] == "inProgress":
            await self.event(type="turn/recovered", thread_id=identity,
                             turn_id=turn["id"], status="inProgress")
        # Acceptance permits attachment regardless of the turn's eventual
        # outcome. It never fabricates normal completion or retries the prompt.
        if self.initial_submitted and has_initial_input(response["thread"], self.initial_input_id):
            for accepted_turn in response["thread"]["turns"]:
                if has_user_message(accepted_turn, self.initial_input_id):
                    await self.initial_ack({"turn": accepted_turn})
            self.ready.set()

    async def connected(self, rpc):
        if not self.initial_attempted:
            await self.start(rpc)
        elif self.conversation:
            await self.bind(rpc, recover=True)
        while True:
            await self.inventory(rpc)
            if self.delivery is not None:
                await self.delivery.poll(rpc)
            if not self.ready.is_set():
                # The accepted input item can become visible after the first
                # recovery snapshot. Keep checking its identity, never resend.
                await self.bind(rpc, recover=True)
            await asyncio.sleep(.25)

    async def observe(self, path):
        while True:
            try:
                async with connect(path, self.notification) as rpc:
                    await self.connected(rpc)
            except FileNotFoundError:
                if self.initial_attempted:
                    await self.problem("Control socket unavailable; reconnecting")
            except (OSError, ConnectionClosed, ConnectionError, TimeoutError) as exc:
                await self.problem(f"Control reconnect: {exc}")
            except (ProtocolError, KeyError, TypeError, ValueError) as exc:
                await self.problem(f"Codex compatibility: {exc}")
            await asyncio.sleep(.25)


def selected_conversation(response, expected):
    identity = response["thread"]["id"]
    if not isinstance(identity, str) or not identity:
        raise ProtocolError("invalid conversation identity")
    if expected is not None and identity != expected:
        raise ProtocolError("resume selected another conversation")
    return identity


def recovered_turn(thread):
    turns = thread["turns"]
    if not isinstance(turns, list):
        raise ProtocolError("resumed thread has unreadable turns")
    if not turns:
        if thread["status"]["type"] == "idle":
            return None
        raise ProtocolError("active resumed thread has no current turn")
    turn = validated_turn(turns[-1])
    expected = {"active": {"inProgress"}, "idle": {"completed", "failed", "interrupted"},
                "systemError": {"failed"}}[thread["status"]["type"]]
    if turn["status"] not in expected:
        raise ProtocolError("resumed thread has inconsistent lifecycle status")
    return turn


def validated_turn(turn):
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
        raise ProtocolError("resumed thread has an unreadable turn")
    if turn.get("status") not in {"inProgress", "completed", "failed", "interrupted"}:
        raise ProtocolError("resumed turn has an unreadable lifecycle status")
    return turn


def has_initial_input(thread, client_id):
    return any(has_user_message(validated_turn(turn), client_id) for turn in reversed(thread["turns"]))


def has_user_message(turn, client_id):
    items = turn.get("items")
    if not isinstance(items, list):
        raise ProtocolError("resumed turn has unreadable items")
    return any(isinstance(item, dict) and item.get("type") == "userMessage"
               and item.get("clientId") == client_id for item in items)


def main_lifecycle_turn(params, method, conversation):
    if not isinstance(params, dict):
        raise ProtocolError("Malformed lifecycle payload")
    if params.get("threadId") != conversation:
        return None
    turn = params.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
        raise ProtocolError("Malformed main turn payload")
    expected = "inProgress" if method == "turn/started" else "completed"
    if turn.get("status") != expected:
        raise ProtocolError("Main turn did not report a normal lifecycle status")
    return turn


def backend_command(arguments, path, worktree):
    settings = {"model": arguments.model, "approval_policy": "never",
                "sandbox_mode": "danger-full-access", "projects": {worktree: {"trust_level": "trusted"}},
                "notify": [str(Path(worktree) / ".agent" / "stop-hook.sh")]}
    if arguments.effort:
        settings["model_reasoning_effort"] = arguments.effort
    command = ["codex", "app-server", "--listen", f"unix://{path}"]
    for key, value in settings.items():
        # The TOML projects table is intentionally inline; JSON object syntax
        # isn't TOML. JSON escaping is valid for TOML basic string values.
        if key == "projects":
            value = "{" + json.dumps(worktree) + '={trust_level="trusted"}}'
        else:
            value = json.dumps(value)
        command.extend(["-c", f"{key}={value}"])
    return command


async def stop_process(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            process.kill()
            await process.wait()


async def run(arguments, client, binding, directory):
    backend_path = str(Path(directory) / "backend.sock")
    gateway_path = str(Path(directory) / "terminal.sock")
    backend = await asyncio.create_subprocess_exec(
        *backend_command(arguments, backend_path, binding["worktree"]),
        stdin=asyncio.subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
    controller = Controller(client, binding, arguments)
    observation = asyncio.create_task(controller.observe(backend_path))
    service_end = asyncio.create_task(backend.wait())
    ready = asyncio.create_task(controller.ready.wait())
    terminal = None
    try:
        done, _ = await asyncio.wait([ready, service_end, observation], return_when=asyncio.FIRST_COMPLETED)
        if service_end in done or observation in done:
            return 1
        gateway = Gateway(backend_path, controller.conversation, client, controller.binding)
        controller.delivery = DeliveryPump(client, controller.binding, gateway.input_lock)
        async with unix_serve(gateway.serve, path=gateway_path):
            terminal = await asyncio.create_subprocess_exec(
                "codex", "--remote", f"unix://{gateway_path}", "-C", binding["worktree"],
                "resume", "--", controller.conversation)
            terminal_end = asyncio.create_task(terminal.wait())
            done, _ = await asyncio.wait([service_end, terminal_end], return_when=asyncio.FIRST_COMPLETED)
            return 1 if service_end in done else terminal.returncode
    finally:
        observation.cancel()
        ready.cancel()
        await asyncio.gather(observation, ready, return_exceptions=True)
        if controller.delivery is not None:
            await controller.delivery.close()
        if terminal is not None:
            await stop_process(terminal)
        await stop_process(backend)
        await controller.event(type="service", status="dead")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--effort", default="")
    parser.add_argument("--resume")
    arguments = parser.parse_args(argv)
    arguments.prompt = Path(arguments.prompt_file).read_text()
    client = BoundClient(os.environ["AGENT_OPS_STATE_DIR"], os.environ["AGENT_OPS_TARGET"],
                         int(os.environ["AGENT_OPS_ISSUE"]), os.environ["AGENT_OPS_LAUNCH_ID"])
    snapshot = client.view()
    if (not valid_snapshot(snapshot) or snapshot["retired"]
            or snapshot["binding"]["runtime"] != "codex"):
        raise RuntimeError("supervisor requires a current non-retired Codex launch")
    binding = snapshot["binding"]
    if binding["conversation_id"] != arguments.resume:
        raise RuntimeError("resume must match the prepared conversation")
    with tempfile.TemporaryDirectory(prefix="agent-ops-") as directory:
        return asyncio.run(run(arguments, client, binding, directory))


if __name__ == "__main__":
    raise SystemExit(main())
