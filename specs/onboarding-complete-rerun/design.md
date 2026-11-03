# Prompt-free completed onboarding reruns — Design

## Decisions

- **Completed records are informational reruns** — use the persisted terminal status before collecting commands; deployment reconciliation remains outside this command's completed-state behavior.
- **Reuse existing status presentation** — show stored steps plus an explicit already-onboarded notice, without persisting a display-only notice or starting the service.
- **Preserve existing entry validation and open-record flow** — terminal-state handling adds no new permission or onboarding transition.
- **Drive acceptance through the CLI entry point** — use temporary real configuration and record files, closed or inspectable stdin, and isolated external-service boundaries; no live infrastructure changes are required.

## Data model

No schema changes or new DB constraints. The existing records are JSON files, not database rows.

| Table | Field | Constraint | Why |
|---|---|---|---|
| Existing onboarding JSON record | status | Existing open / complete state contract; unchanged | Only complete permits the informational rerun |
| Existing onboarding JSON record | request, steps, notice and remaining fields | Completed rerun preserves persisted bytes | Reporting must not rewrite established onboarding state |

## Identities

None.

## Seams

- **Existing:** agent-ops project add <name> <repo>... --board <owner>/<number> --config <path>, exposed by dispatcher.onboard_cli.main(argv, sh). Tests exercise the CLI's observable output, return status, stdin consumption, persisted files and service boundary. Include a subprocess invocation with closed stdin to reproduce the end-user failure.
