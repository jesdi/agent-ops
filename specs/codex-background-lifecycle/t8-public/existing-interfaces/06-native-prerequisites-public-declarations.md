# Owned native prerequisites and outstanding primary artifacts

This supplement declares existing interfaces. T8 implementation and genuine native
actual-launch verification remain required; settled behavior is in the
[presentation contract](../../runtime-presentation-contract.md).

The earlier user Tailscale sign-in and box baseline were verified; the owned target
is the existing SSH alias `box`. The latest read-only reconnect succeeded with the
same native version baseline. This access metadata does not establish T8 integration.
The verified merged-T7 source and owned run identity are recorded in the
[public declarations index](../README.md).

| Prerequisite | Verified baseline identity/location |
| --- | --- |
| Codex | 0.156.1; `/home/agent/.local/lib/codex/0.156.1/bin/codex`; full package root `/home/agent/.local/lib/codex/0.156.1`, with `codex-package.json` |
| Claude Code | 2.1.288; `/home/agent/.local/share/claude/versions/2.1.288` |
| herdr | 0.9.1; `/home/agent/.local/bin/herdr` |
| Podman | 5.7.0; `/usr/bin/podman` |
| cached session image | content identity `sha256:3f2fc7860fd39724605a062b20836ff42995fa74bfb21069d24a206e62343bc3` |

`AGENT_OPS_SESSION_IMAGE` and `AGENT_OPS_HERDR` are the existing selection interfaces. Container-native destinations and provider homes are declared with the mount builders. Selected Codex supervisor dependencies include genuine `websockets` asyncio/sync support compatible with the image Python; interpreter/package capability must be verified against the actual image, not inferred from a host binary or schema alone. No install/update/pull is authorized by this candidate.

Generic scripted model providers are owned local external services, with isolated native homes, no real account credentials and generic payloads only. They must serve genuine native model protocols; they are not replacements for the CLI/TUI, hook, worker, history, controller or receipt APIs. No private provider implementation/preparation bundle is part of this declaration package.

Existing genuine native CLI interfaces observed on these versions:

```text
codex app-server --listen unix://<absolute-owned-socket>
codex --remote unix://<absolute-owned-gateway-socket> --no-alt-screen -C <owned-worktree>
claude -p --input-format stream-json --output-format stream-json --verbose --dangerously-skip-permissions --model <model>
```

The Claude stream-json invocation is a native capability interface, with disclosed flags; it is not an unchanged production Sessions launch. Production's Claude Remote Control flag requires its supported authentication context and must not silently be counted as exercised by the no-auth headless interface.

The [native schema subset manifest](native-schemas/manifest.json) records the 20
directly referenced Codex experimental schema documents copied byte-for-byte from
the genuine Darwin Codex 0.156.1 bundle. It records every original file hash and
the upstream full-manifest hash and count (436 files). The upstream full bundle is
retained off-repository; its catalog-only schema is not part of this publication.
Source-binding/capability validation of the actual Linux binary remains separate;
schema content does not prove native semantics.

Required direct public schema pointers include:

- [ClientRequest](native-schemas/ClientRequest.json), [ServerNotification](native-schemas/ServerNotification.json), [JSONRPCMessage](native-schemas/JSONRPCMessage.json).
- [InitializeParams](native-schemas/v1/InitializeParams.json), [InitializeResponse](native-schemas/v1/InitializeResponse.json).
- [ThreadStartParams](native-schemas/v2/ThreadStartParams.json), [ThreadResumeParams](native-schemas/v2/ThreadResumeParams.json), [ThreadListParams](native-schemas/v2/ThreadListParams.json), [ThreadReadResponse](native-schemas/v2/ThreadReadResponse.json).
- [ThreadBackgroundTerminalsListResponse](native-schemas/v2/ThreadBackgroundTerminalsListResponse.json), [ThreadItemsListResponse](native-schemas/v2/ThreadItemsListResponse.json), [ThreadTurnsListResponse](native-schemas/v2/ThreadTurnsListResponse.json).
- [TurnStartParams](native-schemas/v2/TurnStartParams.json), [TurnSteerParams](native-schemas/v2/TurnSteerParams.json), [TurnStartedNotification](native-schemas/v2/TurnStartedNotification.json), [TurnCompletedNotification](native-schemas/v2/TurnCompletedNotification.json).
- [ItemStartedNotification](native-schemas/v2/ItemStartedNotification.json), [ItemCompletedNotification](native-schemas/v2/ItemCompletedNotification.json), [CommandExecutionOutputDeltaNotification](native-schemas/v2/CommandExecutionOutputDeltaNotification.json), [HookCompletedNotification](native-schemas/v2/HookCompletedNotification.json).

Genuine generic advertised tool declaration projections are also included: [Codex code-mode/agent tools](native-tools/codex-0.156.1-tool-declarations.json), [Codex command stdin/status types](native-tools/codex-0.156.1-command-tools.d.ts), [Claude Bash/Agent input schemas](native-tools/claude-2.1.288-tool-declarations.json). They contain native declarations only, with no generic fixture commands/prompts, captured IDs, provider responses, orchestration or helper algorithms. Codex raw code-mode `functions.exec` input follows its included native Lark grammar; agent calls use the included function schemas. Claude tools use native `input_schema`.

The captured Codex model request carries `model:string`, `input:array`, `tool_choice`, `parallel_tool_calls:boolean`, `reasoning:object`, `store:boolean`, `stream:boolean`, `include:array`, `prompt_cache_key:string`, `text:object`, `client_metadata:object`. Native additional tool advertisement is an input record with `type:"additional_tools"`, `id:string`, `role:"developer"`, `tools:array`; its nested namespace/custom/function tool records follow the included declarations. Client metadata includes native string `thread_id`, `session_id`, `turn_id`, `root_turn_id` and native x-codex metadata fields. Captured Claude Messages requests carry `model:string`, `messages:array`, `system:array`, `tools:array`, `metadata:object`, `max_tokens:integer`, `thinking:object`, `context_management:object`, `stream:boolean`. These generic record shapes are declarative projections, not provider response recipes or proof that every request option is required.

The pinned local-provider configuration, public model streaming interfaces and
owned herdr 0.9.1 lifecycle/selectors are declared in
[external prerequisites](public-external-prerequisites.md), supported by the
[raw herdr selector help](native-herdr-terminal-help.txt). These close the earlier
declaration gaps; genuine native execution and final acceptance remain required.
