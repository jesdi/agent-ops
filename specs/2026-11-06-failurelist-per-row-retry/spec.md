# FailureList: per-row Retry state and names: Spec

## Requirements

1. **Per-task busy state.** A quarantined task's Retry button is disabled while a retry
   request for that same task (same `repository` and `task_issue`) is in flight, and enabled
   otherwise. A retry in flight for any other task never disables it. Task identity is the
   key `${repository}#${task_issue}`, the same key the `errors` prop already uses, because
   issue numbers are per-repository.
2. **Concurrent retries.** Retries for different tasks can be in flight at the same time.
   Each button re-enables when its own request settles (success or error), whatever the
   other requests are doing.
3. **Named Retry rows.** Each existing quarantined task row has `role="group"` and
   accessible name `${repository}#${task_issue}`, for example `widget#38`, following
   ADR 0007. Each button's accessible name and visible text stay `Retry`. Tests select
   a row's button with `within(getByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })`.
4. **Existing behaviour is kept.** A retry still POSTs `/api/task/{repository}/{issue}/retry`
   for its row, and a failed retry still shows the API `detail` inline in that row
   (`data-testid="retry-error-<task_issue>"`). The existing tests in
   `frontend/src/pages/__tests__/FailuresPage.test.tsx` that query
   `getByRole('button', { name: 'Retry' })` are updated to scope the Retry query to the task-specific group.
   This is a test change only, and their assertions stay the same.

## Scenarios

The fixture for scenarios 1–5 is two quarantined tasks that share an issue number on
different repositories: `widget` task #38 (blocker #39) and `gadget` task #38 (blocker #40),
served by `GET /api/failures`.

### Scenario: reproduction: an in-flight retry disables only its own row (req 1)

Committed as the failing test `an in-flight retry disables only its own row`.

- **Given** the Failures page shows `widget` task #38 and `gadget` task #38, and
  `POST /api/task/widget/38/retry` is held open
- **When** the operator clicks Retry on the `widget` row
- **Then** the `widget` Retry button is disabled and the `gadget` Retry button stays enabled
  (today both are disabled)
- **And when** the held request then answers 202, the `widget` button is enabled again

### Scenario: reproduction: each Retry row names its task (req 3)

Committed as the failing test `each Retry row names its task and keeps the Retry button name`.

- **Given** the Failures page shows `widget` task #38 and `gadget` task #38
- **When** the buttons are queried by role
- **Then** the groups named exactly `widget#38` and `gadget#38` each contain a button
  named exactly `Retry`
- **And** each of those buttons still shows the visible text `Retry`

### Scenario: same issue number on another repository does not share busy state (req 1)

- **Given** the two-task fixture and `POST /api/task/gadget/38/retry` held open
- **When** the operator clicks Retry on the `gadget` row
- **Then** the `gadget` button is disabled and the `widget` button stays enabled

### Scenario: two retries in flight settle independently (req 2)

- **Given** the two-task fixture with both retry endpoints held open
- **When** the operator clicks Retry on `widget`, then on `gadget`
- **Then** both buttons are disabled
- **When** only the `widget` request answers 202
- **Then** the `widget` button is enabled and the `gadget` button is still disabled

### Scenario: a failed retry re-enables its button and shows its error (reqs 1, 4)

- **Given** the two-task fixture and `POST /api/task/widget/38/retry` answering 404
  `{"detail": "issue 38 is not quarantined"}`
- **When** the operator clicks `Retry` within the `widget#38` group
- **Then** that row shows `issue 38 is not quarantined` inline, and the `widget` button is
  enabled again

### Scenario: retry still posts the intent (req 4)

- **Given** the default single-task fixture (`widget` task #38)
- **When** the operator clicks `Retry` within the `widget#38` group
- **Then** `POST /api/task/widget/38/retry` is called

## Out of scope

- GhostCard (#84) and collapsed-column markers (#85).
- The visible button label, the retry endpoint, and the error-display convention.

## Notes for the plan stage

- Seam: the page-level test `frontend/src/pages/__tests__/FailuresPage.test.tsx`, which
  renders `FailuresPage` against MSW handlers through the real API client and React Query.
  That's the highest seam that reaches the bug. The Playwright suite (`frontend/e2e/`) has
  no Failures spec, and its fake API serves an empty quarantine list. The two reproduction
  tests already live there and are red on `main`'s code.
- `FailureList`'s `busy?: boolean` prop can't express requirement 1 and has to become
  per-task, keyed like `errors`. A single `useMutation`'s `isPending`/`variables` only track
  the latest call, so requirement 2 needs per-call tracking (for example
  `useMutationState`, or a set maintained in `onMutate`/`onSettled`). The plan picks the
  mechanism.
