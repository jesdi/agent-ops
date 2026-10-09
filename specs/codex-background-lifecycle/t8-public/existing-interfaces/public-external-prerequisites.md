# Pinned local-provider and owned herdr declarations

These interfaces and observed baseline settings close the external declaration
gaps in the [native prerequisites](06-native-prerequisites-public-declarations.md).
T8 implementation and genuine native actual-launch verification remain required;
these declarations make no integration success claim.

## Codex 0.156.1 configuration

The observed generic local baseline uses an owned `CODEX_HOME/config.toml`.
It selects `model_provider = "fixture"` and model `gpt-6-astra`. The provider
record has `name`, loopback `base_url = "http://127.0.0.1:<port>/v1"`,
`wire_api = "responses"`, `requires_openai_auth = false` and
`supports_websockets = false`. The last setting controls model transport;
the app-server/Gateway retains its declared WebSocket transport.

Other observed settings: `web_search = "disabled"`; features `hooks`,
`multi_agent` and `code_mode_host` enabled; `plugins`, `apps`,
`enable_request_compression` and `shell_snapshot` disabled;
`skip_host_skill_discovery` enabled. The owned generic project has
`trust_level = "trusted"`. These are pinned baseline declarations, not a
claim that every switch suppresses all external traffic. Final execution uses
isolated HOME/XDG/provider directories and no real credentials.

The current official [provider configuration reference](https://learn.chatgpt.com/docs/config-file/config-advanced)
describes provider selection, endpoint and Responses transport. The pinned
baseline establishes the observed version; current documentation alone does
not establish compatibility with 0.156.1.

## Model streaming interfaces

For the local Responses endpoint, requests use `POST /v1/responses` and
`stream: true`. SSE records carry an event name and JSON data with its `type`.
The [official streaming reference](https://developers.openai.com/api/reference/resources/responses/streaming-events)
declares response lifecycle, indexed output items, text/argument deltas and
terminal events. Function argument events carry `item_id`, `output_index`,
`sequence_number` and either `delta` or final `arguments`. Failed responses
carry a failed response object with its error. Output-item schemas, including
native advertised custom tools, remain the referenced native/API declarations;
an incomplete or erroneous stream is not a successful worker result.

Claude 2.1.288's observed local baseline uses an owned `CLAUDE_CONFIG_DIR`,
loopback `ANTHROPIC_BASE_URL`, and the literal dummy `ANTHROPIC_API_KEY`
`local-fixture-key`. This is a noncredential consumed by the local fixture,
not an authenticated Anthropic account. Both observed traffic switches are
`DISABLE_NON_ESSENTIAL_TRAFFIC=1` and
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.
The native invocation declares `-p --dangerously-skip-permissions --model
claude-haiku-4-5 --input-format stream-json --output-format stream-json
--verbose`. Baseline HOME was inherited; final acceptance must use isolated
HOME and must disclose the headless fallback separately from production
Remote Control.

The [official gateway reference](https://code.claude.com/docs/en/llm-gateway)
declares the endpoint/authentication interface. Local Messages requests use
`POST /v1/messages`; the [official streaming reference](https://platform.claude.com/docs/en/build-with-claude/streaming)
declares `message_start`, indexed content-block start/delta/stop events,
`message_delta`, and terminal `message_stop`, with optional ping/error events.
Tool-use blocks identify `id`, `name` and object `input`; streamed input deltas
use `input_json_delta.partial_json`. Text deltas use `text_delta.text`.
Message deltas carry the stop reason and usage. An error carries typed error
and message fields. These are public wire declarations, not fixture prompts,
tool programs, response recipes or proof of actual final integration.

## Owned herdr 0.9.1 process and configuration

Observed native commands are `herdr --session <OWN> server`,
`herdr --session <OWN> status server`, and
`herdr --session <OWN> server stop`. Bare status uses the same selected
session through `HERDR_SESSION`. There is no observed `server start`
subcommand in this baseline.

The owned configuration uses `HERDR_CONFIG_PATH` and private
`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` and
`XDG_RUNTIME_DIR`. `HERDR_SOCKET_PATH` and `HERDR_CLIENT_SOCKET_PATH` select
`<XDG_CONFIG_HOME>/herdr/sessions/<OWN>/{herdr.sock,herdr-client.sock}`.
The baseline config declares onboarding false; update version/manifest checks
false; terminal default shell and new cwd inside the owned workspace,
`shell_mode = "non_login"`; session `resume_agents_on_restore = false`;
UI sound disabled and toast delivery off; experimental pane history false.

Readiness requires a successful native status for the exact owned server,
version 0.9.1, compatible endpoint/private protocol and the expected socket,
in addition to a live owned process. A path or failed status is insufficient.
Shutdown targets only that owned session and its recorded process lifetime.
Terminal absence requires a successful exact-session native tab inventory
while the server is still healthy; a degraded lookup returning None is not
proof. This declares existing external lifetime/configuration, with no new
product shutdown seam or authority to touch another server.

Exact native terminal selectors, rechecked on that same box binary:
`herdr session attach <NAME>` attaches its named persistent session;
`herdr --session <OWN> tab focus <tab_id>` selects the observed exact tab;
`herdr --session <OWN> tab list [--workspace <WORKSPACE_ID>]` lists its tabs.
The healthy tab-list JSON envelope is
`{id:string,result:{type:'tab_list',tabs:[{tab_id:string,workspace_id:string,
label:string,number:integer,pane_count:integer,focused:boolean,
agent_status:string}]}}`. IDs are observed from the native inventory/returned
Tab handle, not inferred from labels. The genuine client needs its terminal
streams; CLI help is declaration evidence only, not an attached-terminal test.
The [raw selector help](native-herdr-terminal-help.txt) contains no runtime
state or implementation. The existing SDK Tab declarations remain the actual
Sessions launch interface; these commands add no reconstructed session launch.
