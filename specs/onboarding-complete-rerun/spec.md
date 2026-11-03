# Prompt-free completed onboarding reruns

## Requirements

1. For a stored complete record, project add returns success without prompting or reading stdin, including when stdin is closed.
2. The completed rerun displays the project status, recorded steps and notice: already onboarded, even when the saved notice is empty or stale.
3. A completed rerun is informational: it leaves the persisted record unchanged and does not enqueue onboarding intents or start the onboarding service.
4. Missing and open records retain command collection, confirmation and resume behavior; EOF during required command collection still fails.
5. The existing agent-account restriction applies before reporting a completed record.

## Scenarios

### Scenario: Completed onboarding with closed stdin

- **Given** analytics is complete with repository jesdi/analytics, board jesdi/4, gate command make gate, verified steps and an empty notice.
- **When** the agent account runs agent-ops project add analytics jesdi/analytics --board jesdi/4 with closed stdin.
- **Then** exit status is 0, output includes analytics: complete, the recorded steps including visible and notice: already onboarded, with no setup_cmd, gate_cmd, verify_cmd or confirmation prompts and no stdin error.

### Scenario: Completed onboarding does not consume supplied input or mutate state

- **Given** analytics is complete with a saved stale notice and unchanged recorded configuration.
- **When** the agent account reruns project add with available input.
- **Then** input remains unread, the completed status and already-onboarded notice appear, the record remains byte-for-byte unchanged, no onboarding intent is enqueued and no onboarding service is started.

### Scenario: New onboarding still collects commands

- **Given** analytics has no onboarding record.
- **When** the agent account runs project add and supplies make setup, make gate and make e2e followed by declining confirmation.
- **Then** those commands are collected into the onboarding request and the proposal remains unconfirmed.

### Scenario: Incomplete onboarding still resumes interactively

- **Given** analytics has an open record with stored commands and awaits confirmation.
- **When** the agent account reruns project add, accepts its command defaults and confirms the shown revision.
- **Then** the stored command defaults are offered and confirmation of the shown revision proceeds through the existing engine flow.

### Scenario: EOF remains an error for unfinished command collection

- **Given** analytics has no record or an open record requiring command input.
- **When** project add runs with closed stdin.
- **Then** it reports stdin ended before every command was given and does not enqueue an add intent or start the service.

### Scenario: Completed status does not bypass account validation

- **Given** analytics is complete and the current account is jesdi rather than agent.
- **When** project add is invoked.
- **Then** it returns 2 with the existing instruction to run as agent, without reading stdin or starting onboarding.

## Out of scope

Changing onboarding engine transitions, editing completed projects, configuration or record schema changes, deployment reconciliation, and permission changes.
