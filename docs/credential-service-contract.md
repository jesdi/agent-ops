# Dispatcher credential service contract

Ticket 07 implements the application side. The later infrastructure run must
provide the service and its GitHub App/grant verification; until then scoped
launches fail visibly without reverting to host authentication.

The dispatcher uses `AGENT_OPS_CREDENTIAL_SOCKET` (default
`/run/agent-ops-credentials/dispatcher.sock`). Only the trusted dispatcher may
connect. Neither this socket nor App keys, board secrets, or control secrets
may be mounted in execution containers. All requests are JSON HTTP POST:

- `/v1/executions/register`: `execution_id`, `project`, `repositories` (canonical
  repository names), `purpose` (`task` or `triage`). Resolve repository IDs from
  trusted installation metadata, enforce the configured project registry, and
  persist the immutable execution scope. Re-registering the same scope is
  idempotent; changing its scope is refused.
- `/v1/acquire` and `/v1/refresh`: `execution_id`, `repository_id` (ID or recorded
  canonical name), `purpose`. Enforce the ExecutionCredentials seam's registered
  scope, permission bounds and capability verification. Never derive authority
  from a caller's repository claim. Refresh works across dispatcher restarts.
  Response: `token`, `repository_id`, `permissions`, `expires_at` (Unix seconds).
- Trusted `dispatcher` board/control requests use the same acquire endpoint.
  Their authorization comes from the service's registered boards and secret
  references, never execution registration. Responses never contain App keys.

Successful responses have status 200. Non-200 responses and unavailable service
connections fail the operation; no host-login fallback is permitted. A blank
`token` is refused like a missing one.

A refusal names its reason in a JSON body `{"code": ..., "detail": ...}`, so the
operator sees which action unblocks the grant. `detail` is a short, safe
explanation and must never contain a token. The codes:

| `code` | Meaning |
|---|---|
| `denied` | The caller is not authorized for this credential. |
| `installation_missing` | The GitHub App is not installed for the owner. |
| `repository_not_granted` | The App installation has no access to the repository. |
| `organization_approval_required` | An organization administrator must approve the App. |
| `grant_unresolved` | The deployment grant could not be resolved. |
| `token_generation_failed` | GitHub refused to generate the token. |

A body without a known code gives a generic failure with the HTTP status.

Scoped task launches mount a private token directory read-only at
`/run/agent-ops`. Replacement uses atomic rename so the next invocation sees the
complete new token. Read-only `gh` and `git` wrappers precede system commands in
PATH. The image must provide `/usr/bin/gh`, `/usr/bin/git`, and `/bin/sh`.
`gh` reads GH_TOKEN_FILE on every invocation; Git overrides inherited credential
helpers to invoke that same reader. Empty or missing token files fail closed.

For the existing targets-only installation, the operator may explicitly set
`AGENT_OPS_LEGACY_CREDENTIALS=1` while verifying replacement App grants. This
preserves the old host login and live consumers; it does not claim migration
success. This flag is refused when `projects` is configured. Remove it only
after replacement capability checks pass and existing consumers have drained.
Service failures never enable it. Existing running legacy containers are not
rewritten or revoked by a dispatcher upgrade.
