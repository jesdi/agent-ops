"""Own the Codex service and attach its real terminal to one bound root."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import asynccontextmanager
from uuid import uuid4

from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed

from dispatcher.codex_transport import Gateway, ProtocolError, connect
from dispatcher.runtime_http import BoundClient
from dispatcher.runtime_bootstrap import initial_lifecycle_ready
from dispatcher.runtime_snapshots import valid_snapshot
from dispatcher.codex_inventory import NativeInventory
from dispatcher.codex_delivery import DeliveryPump
from dispatcher.codex_ownership import listener_call
from dispatcher.codex_receipts import reconcile


class Controller:
    def __init__(self, client, binding, arguments, *, input_lock=None, mode='first-launch'):
        self.client, self.binding, self.arguments = client, binding, arguments
        self.conversation = binding["conversation_id"]
        self.mode = mode
        self.input_lock = input_lock if input_lock is not None else asyncio.Lock()
        self.ready = asyncio.Event()
        self.work = NativeInventory()
        self.delivery = DeliveryPump(client, binding, self.input_lock)
        self.pending = set()
        self.initial_input_id = None
        self.root_attempt = None
        self.root_reply = None

    async def view(self):
        snapshot = await listener_call(self.client.view)
        if (not valid_snapshot(snapshot) or snapshot['retired']
                or snapshot['service'] == 'dead'):
            raise RuntimeError('Controller requires the same current live launch')
        self.adopt_root_reply(snapshot)
        if (snapshot['binding'] != self.binding or self.root_reply is not None
                and (snapshot.get('bootstrap') or {}).get('root_status') == 'bound'):
            raise RuntimeError('Controller requires the same current live launch')
        return snapshot

    def adopt_root_reply(self, snapshot):
        if self.root_reply is None:
            return
        expected = dict(self.binding, conversation_id=self.root_reply)
        record = dict(self.root_attempt, root_status='bound')
        if snapshot['binding'] == expected and snapshot.get('bootstrap') == record:
            self.binding = expected
            self.conversation = self.root_reply
            self.delivery.binding = expected
            self.root_reply = None

    async def event(self, **event):
        return await listener_call(self.client.event, self.binding, event) is True

    async def problem(self, message):
        try:
            await self.event(type="control/unknown", message=message)
        except (OSError, ConnectionError, TimeoutError):
            # The listener can be the unavailable transport being reported.
            # Preserve the observer so its next exact view can recover.
            pass

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
        if accepted and method == 'turn/started' and initial_lifecycle_ready(await self.view()):
            self.ready.set()

    def root_params(self):
        params = dict(cwd=self.binding["worktree"], model=self.arguments.model,
                      approvalPolicy="never", sandbox="danger-full-access")
        if self.arguments.effort:
            params["config"] = {"model_reasoning_effort": self.arguments.effort}
        if self.conversation is not None:
            params['threadId'] = self.conversation
        return params

    async def bind(self, rpc, *, operation=None):
        method = 'thread/resume' if self.conversation is not None else 'thread/start'
        async def received(response):
            if operation is None:
                await self.recover(response)
                return
            identity = selected_conversation(response, self.conversation)
            self.root_reply = identity
            if not await self.event(type='bound', conversation_id=identity, root_operation_id=operation):
                raise ProtocolError('Listener rejected root operation binding')
            await self.view()
            if self.root_reply is not None:
                raise ProtocolError('Uncorrelated refined launch binding')
        await rpc.call(method, self.root_params(), on_result=received)

    async def start(self, rpc):
        async with self.input_lock:
            snapshot = await self.view()
            if 'bootstrap' not in snapshot or snapshot['bootstrap'] is not None:
                return
            operation = str(uuid4())
            identity = 'agent-ops-bootstrap:' + self.binding['launch_id']
            attempted = dict(type='bootstrap/root-attempted', observed_revision=snapshot['revision'],
                root_operation_id=operation, root_method='thread/resume' if self.conversation else 'thread/start',
                requested_conversation_id=self.conversation, client_message_id=identity,
                input=[dict(type='text', text=self.arguments.prompt)])
            if not await self.event(**attempted):
                return
            self.root_attempt = dict(root_operation_id=operation,
                root_method=attempted['root_method'],
                requested_conversation_id=attempted['requested_conversation_id'],
                root_attempt_revision=snapshot['revision'] + 1, root_status='attempted-unconfirmed',
                initial_input=dict(client_message_id=identity, input=attempted['input']))
        await self.bind(rpc, operation=operation)
        await self.event(type='service', status='live')
        await self.seed(rpc)
        await self.submit_initial(rpc)

    async def submit_initial(self, rpc):
        async with self.input_lock:
            snapshot = await self.view()
            record = snapshot.get('bootstrap')
            if not isinstance(record, dict) or record['root_status'] != 'bound':
                return
            initial = record['initial_input']
            identity = initial['client_message_id']
            if identity in snapshot['inputs']:
                return
            if not await self.event(type='bootstrap/sent', observed_revision=snapshot['revision'],
                    root_operation_id=record['root_operation_id'], client_message_id=identity,
                    thread_id=self.conversation):
                return
            params = dict(threadId=self.conversation, model=self.arguments.model,
                          clientUserMessageId=identity, input=initial['input'])
            if self.arguments.effort:
                params['effort'] = self.arguments.effort
            self.initial_input_id = identity
            request = await rpc.begin('turn/start', params, on_result=self.initial_ack)
            task = asyncio.create_task(self.initial_reply(rpc, request))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def initial_reply(self, rpc, request):
        try:
            await rpc.finish(request)
        except (OSError, ConnectionError, ConnectionClosed, TimeoutError, ProtocolError):
            return

    async def initial_ack(self, result):
        if not isinstance(result, dict) or not isinstance(result.get('turn'), dict):
            return
        turn = result['turn'].get('id')
        if not isinstance(turn, str) or not turn:
            return
        if await self.event(type='input/accepted', client_message_id=self.initial_input_id, turn_id=turn):
            self.ready.set()

    async def seed(self, rpc):
        snapshot = await self.view()
        if (snapshot.get('history_checkpoint') or {}).get('seeded'):
            return
        checkpoint = dict(launch_id=self.binding["launch_id"], seeded=False,
                          baseline_turns=[], baseline_workers=[], seen_completions=[], scopes=[])
        await self.event(type="inventory", certainty="unknown", workers=[], history_checkpoint=checkpoint)
        await self.inventory(rpc, seed=True)

    async def inventory(self, rpc, *, seed=False):
        snapshot = await self.view()
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
        if turn is not None and turn['status'] == 'inProgress':
            await self.event(type='turn/recovered', thread_id=identity, turn_id=turn['id'], status='inProgress')
        # Resume is a subscription and lifecycle observation. Receipt proof uses
        # its own exhausted items scope outside this ordered receiver callback.

    async def receipts(self, rpc):
        snapshot = await self.view()
        try:
            await reconcile(rpc, snapshot, lambda event: self.event(**event))
        except (ProtocolError, KeyError, TypeError, ValueError):
            pass
        snapshot = await self.view()
        initial = (snapshot.get('bootstrap') or {}).get('initial_input', {})
        receipt = snapshot['inputs'].get(initial.get('client_message_id'), {})
        if receipt.get('status') in ('accepted', 'settled') or initial_lifecycle_ready(snapshot):
            self.ready.set()

    async def connected(self, rpc):
        snapshot = await self.view()
        if self.mode == 'first-launch' and snapshot.get('bootstrap', 'absent') is None:
            await self.start(rpc)
        elif self.conversation is not None:
            await self.bind(rpc)
            await self.finish_initial(rpc)
        while True:
            await self.view()
            if self.conversation is not None:
                await self.receipts(rpc)
                await self.inventory(rpc)
                await self.delivery.poll(rpc)
            await asyncio.sleep(.25)

    async def finish_initial(self, rpc):
        if self.mode != 'first-launch':
            return
        snapshot = await self.view()
        record = snapshot.get('bootstrap')
        if not isinstance(record, dict) or record['root_status'] != 'bound':
            return
        if record['initial_input']['client_message_id'] in snapshot['inputs']:
            return
        await self.event(type='service', status='live')
        await self.seed(rpc)
        await self.submit_initial(rpc)

    async def observe(self, path):
        while True:
            try:
                snapshot = await self.view()
                if self.conversation is None and not (self.mode == 'first-launch' and snapshot.get('bootstrap', 'absent') is None):
                    await asyncio.sleep(.25)
                    continue
                async with connect(path, self.notification) as rpc:
                    await self.connected(rpc)
            except FileNotFoundError:
                pass
            except (OSError, ConnectionClosed, ConnectionError, TimeoutError) as exc:
                await self.problem(f"Control reconnect: {exc}")
            except (ProtocolError, KeyError, TypeError, ValueError) as exc:
                await self.problem(f"Codex compatibility: {exc}")
            await asyncio.sleep(.25)

    async def close(self):
        tasks = list(self.pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.delivery.close()


class ControllerAttachment:
    def __init__(self, controller, path):
        self.controller = controller
        self.closed = asyncio.Event()
        self.failure = None
        self.observer = asyncio.create_task(self.observe(path))

    async def observe(self, path):
        try:
            await self.controller.observe(path)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.failure = exc
        finally:
            await self.controller.close()
            self.closed.set()

    async def wait_ready(self):
        ready = asyncio.create_task(self.controller.ready.wait())
        try:
            done, _ = await asyncio.wait((ready, self.observer), return_when=asyncio.FIRST_COMPLETED)
            if self.observer in done:
                if self.failure is not None:
                    raise self.failure
                raise RuntimeError('Controller closed before readiness')
            return (await self.controller.view())['binding']
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)

    async def wait_closed(self):
        await self.closed.wait()
        if self.failure is not None:
            raise self.failure


@asynccontextmanager
async def attach_controller(client, binding, arguments, *, backend_path, input_lock, mode='attach'):
    if mode not in ('attach', 'first-launch') or binding.get('runtime') != 'codex':
        raise ValueError('Invalid controller attachment mode or runtime')
    controller = Controller(client, binding, arguments, mode=mode, input_lock=input_lock)
    await controller.view()
    attachment = ControllerAttachment(controller, backend_path)
    try:
        yield attachment
    finally:
        attachment.observer.cancel()
        await attachment.observer
        if attachment.failure is not None:
            raise attachment.failure


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
    service_end = asyncio.create_task(backend.wait())
    terminal = None
    try:
        async with attach_controller(client, binding, arguments, backend_path=backend_path,
                                     input_lock=asyncio.Lock(), mode='first-launch') as initial:
            ready = asyncio.create_task(initial.wait_ready())
            try:
                done, _ = await asyncio.wait((ready, service_end), return_when=asyncio.FIRST_COMPLETED)
                if service_end in done:
                    return 1
                binding = await ready
            finally:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
        root = binding['conversation_id']
        gateway = Gateway(backend_path, root, client, binding)
        async with attach_controller(client, binding, arguments, backend_path=backend_path,
                                     input_lock=gateway.input_lock) as attachment:
            async with unix_serve(gateway.serve, path=gateway_path):
                terminal = await asyncio.create_subprocess_exec(
                    'codex', '--remote', f'unix://{gateway_path}', '-C', binding['worktree'], 'resume', '--', root)
                terminal_end = asyncio.create_task(terminal.wait())
                closed = asyncio.create_task(attachment.wait_closed())
                try:
                    done, _ = await asyncio.wait((service_end, terminal_end, closed), return_when=asyncio.FIRST_COMPLETED)
                    return terminal.returncode if terminal_end in done else 1
                finally:
                    closed.cancel()
                    await asyncio.gather(closed, return_exceptions=True)
    finally:
        if terminal is not None:
            await stop_process(terminal)
        await stop_process(backend)
        await service_end
        snapshot = await listener_call(client.view)
        if valid_snapshot(snapshot):
            await listener_call(client.event, snapshot['binding'], dict(type='service', status='dead'))


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
