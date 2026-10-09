# Existing installed native hook artifacts and wire inputs

This supplement declares existing interfaces. T8 implementation and genuine native
actual-launch verification remain required; settled behavior is in the
[presentation contract](../../runtime-presentation-contract.md).


```python
def install_stop_hook(wt: str) -> None:
    ...

def create_workspace(target: Target, issue: int, dry_run: bool=False) -> str:
    ...

def remove_workspace(target: Target, wt: str, branch: str, dry_run: bool=False) -> None:
    ...
```

`install_stop_hook(wt)` installs `<wt>/.agent/stop-hook.sh` with executable mode 0755 and updates `<wt>/.claude/settings.local.json`. The resulting `hooks` keys `SessionStart`, `UserPromptSubmit`, `Stop` each hold a list containing a `hooks` list containing an object with `type:"command"` and the following command string (EVENT replaced by the key):

```text
$CLAUDE_PROJECT_DIR/.agent/stop-hook.sh --native-event EVENT
```

The installed settings are the command-discovery artifact. Unrelated settings/hook keys are retained. The installed command, rather than a handwritten surrogate, is the native hook boundary. Claude supplies its native JSON object on stdin and `CLAUDE_PROJECT_DIR` as the project root. The declared argv event and supplied `hook_event_name` must agree for a managed native lifecycle event.

Immutable managed launch environment (strings): `AGENT_OPS_TARGET`, `AGENT_OPS_ISSUE`, `AGENT_OPS_LAUNCH_ID`, `AGENT_OPS_CONVERSATION_ID`, `AGENT_OPS_STAGE`, `AGENT_OPS_TICKET`. Null prepared conversation identity is represented by the empty environment value until native root binding. `AGENT_OPS_STATE_DIR` selects the socket path; absent value uses `$HOME/agent-ops-state`. Native extras are genuine CLI data, not launch identity.

Native Claude input record members used by the installed hook: `hook_event_name:string`, `session_id:string`, optional `prompt_id:string`, optional `agent_id:string`, optional `background_tasks:array`. Genuine common records also carry `cwd`, `transcript_path`, and (after input) `permission_mode`. SessionStart may precede any prompt ID. UserPromptSubmit provides the current prompt ID; its corresponding Stop retains it. Nonempty `agent_id` denotes supplementary child input. Native Stop may carry `stop_hook_active:boolean`, `last_assistant_message:string`, `background_tasks:array`, `session_crons:array`; no native hook output/exit-code/timestamp field is invented. The existing listener accepts running background inventory records with nonempty string `id` and `status:"running"`, retaining genuine additional members.

The installed hook posts `POST /waiting` with JSON object containing `target`, `issue`, `launch_id`, `conversation_id`, `stage`, `ticket`, plus the native members named above when present. Required local executables are bash, Python 3 and curl with Unix socket support. Managed main UserPromptSubmit exits 0 only on successful JSON `true` admission; failed/rejected admission exits 2. SessionStart/Stop and supplementary child handling retain their existing best-effort exit behavior. Codex's supplementary notification argument uses its genuine JSON `thread-id` member; it does not replace the managed Codex supervisor/controller lifecycle.

[Official Claude common hook inputs](https://code.claude.com/docs/en/hooks#common-input-fields) describe prompt and child identity. Exact native/provider schema prerequisites remain separately listed; these generic member declarations do not count as actual native execution.

The recorded genuine 2.1.288 [field-only native identity projection](native-tools/claude-2.1.288-hook-identity-projection.json) preserves the observed SessionStart with no prompt_id, UserPromptSubmit/Stop pairs P1 and P2, one running shell task W1 at both Stops, and the automatic native task-notification UserPromptSubmit with distinct P3 and its matching Stop with empty background_tasks. These labels alias real captured identities; they are not surrogate hook payloads or T8 acceptance evidence. Native shell inventory additionally carried genuine string `type` (value `shell`), `description`, and `command` members. No transcript/provider recipe or actual fixture command is included.
