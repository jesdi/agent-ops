"""Historical Codex execution settings survive the T3 supervisor boundary.

The locked T3 fixture runs the documented executable with real listener and
WebSocket processes. Assertions use only the external Codex argv/RPC contract.
"""
import tomllib

from tests.test_bound_turns_t3_acceptance import isolated, supervisor  # noqa: F401


def test_controlled_codex_preserves_effective_trust_approval_and_sandbox(isolated):
    with supervisor(isolated) as running:
        identity = running.ready()
        backend = running.records("backend-started")[0]["argv"]
        terminal = running.records("terminal-started")[0]["argv"]
        requests = [record["params"] for record in running.records("rpc")
                    if record["method"] in {"thread/start", "thread/resume", "turn/start"}
                    and record["params"].get("threadId", identity) == identity]

        def cli_config(arguments):
            config = {}
            for index, argument in enumerate(arguments):
                if index and arguments[index - 1] in {"-c", "--config"}:
                    key, separator, value = argument.partition("=")
                    if separator:
                        try:
                            config[key] = tomllib.loads("value=" + value)["value"]
                        except tomllib.TOMLDecodeError:
                            config[key] = value
            return config

        backend_config = cli_config(backend)
        terminal_config = cli_config(terminal)
        request_configs = [request.get("config") or {} for request in requests]

        # App-server defaults or an explicit thread/turn policy must preserve
        # the existing container's unrestricted, noninteractive task execution.
        approval_disabled = (
            "--dangerously-bypass-approvals-and-sandbox" in backend
            or backend_config.get("approval_policy") == "never"
            or any(request.get("approvalPolicy") == "never" for request in requests)
            or any(config.get("approval_policy") == "never" for config in request_configs)
        )
        sandbox_disabled = (
            "--dangerously-bypass-approvals-and-sandbox" in backend
            or backend_config.get("sandbox_mode") == "danger-full-access"
            or any(request.get("sandbox") == "danger-full-access" or
                   (request.get("sandboxPolicy") or {}).get("type") == "dangerFullAccess"
                   for request in requests)
            or any(config.get("sandbox_mode") == "danger-full-access" for config in request_configs)
        )
        assert approval_disabled, "the actual Codex backend/root must retain approval policy never"
        assert sandbox_disabled, "the actual Codex backend/root must retain danger-full-access sandbox"

        # The full projects table avoids Codex's dotted -c key interpretation.
        # The setting may reach the backend, the exact root, or the remote TUI.
        configs = [backend_config, terminal_config, *request_configs]
        assert any((config.get("projects") or {}).get(str(running.worktree), {}).get("trust_level")
                   == "trusted" for config in configs), (
            "the running Codex processes must receive the exact task worktree's trusted project setting")
        assert running.records("terminal-started")[0]["thread_id"] == identity
