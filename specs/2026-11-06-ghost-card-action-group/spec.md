# GhostCard action group carries the issue identifier — Spec

Source: issue #84 (jesdi/agent-ops). The issue body settles the design: leave the button names
alone and wrap each card's action row in `<div role="group" aria-label="{target}#{number}">`.
No operator questionnaire was raised; nothing in that design was open.

## Requirements

1. In `GhostCardView`, the row holding the Boost, Demote, Next and Ready buttons is an element
   with `role="group"` whose accessible name is `{ghost.repository}#{ghost.number}` (e.g.
   `widget#73`) — the same identifier string the card header and its `Details for …` expand
   toggle already use (`CompactCard` builds it as `${drag.repository}#${drag.issue}`).
2. All four buttons are descendants of that group, and nothing else interactive is added to it.
3. The buttons' accessible names stay exactly `Boost`, `Demote`, `Next` and `Ready`; existing
   queries such as `getByRole('button', { name: 'Boost' })` in `GhostCard.test.tsx`,
   `BoardPage.test.tsx` and `BoardPage.projects.test.tsx` keep passing unchanged.
4. Behaviour is unchanged: the group exists only while the card is expanded (it lives in the
   expand-only detail block), each button still calls its handler with the same arguments, and
   `busy` still disables all four.
5. Two ghosts whose issue numbers collide across repositories (`alpha#73`, `beta#73`) get
   distinct group names, so the name must include the repository, not only the number.
6. No visual change: the group keeps the existing row classes (`mt-2 flex gap-1`).

## Scenarios

### Scenario: expanded ghost exposes a named action group (R1, R2)

- **Given** a ghost `{ repository: 'widget', number: 73, title: 'Ship dark mode' }` rendered by
  `GhostCardView`
- **When** the operator expands it via the `Details for widget#73` toggle
- **Then** `getByRole('group', { name: 'widget#73' })` exists
- **And** `within(group).getByRole('button', { name })` finds each of `Boost`, `Demote`, `Next`,
  `Ready`
- **And** the group contains exactly those four buttons

### Scenario: button names are unchanged (R3)

- **Given** the expanded `widget#73` ghost
- **When** the test queries `getByRole('button', { name: 'Boost' })` (exact string match)
- **Then** it finds the Boost button, and likewise for `Demote`, `Next` and `Ready`
- **And** the existing GhostCard, BoardPage and BoardPage.projects tests pass without edits

### Scenario: collapsed ghost has no action group (R4)

- **Given** the `widget#73` ghost, not expanded
- **Then** `queryByRole('group', { name: 'widget#73' })` returns null

### Scenario: actions still act from inside the group (R4)

- **Given** the expanded `widget#73` ghost with spy handlers
- **When** the operator clicks Boost, Demote, Next and Ready inside the `widget#73` group
- **Then** `onBoost` is called with `(73, 1)` then `(73, -1)`, `onNext` with `73`, `onReady` with
  `73`

### Scenario: busy still disables every action in the group (R4)

- **Given** the expanded `widget#73` ghost with `busy`
- **Then** all four buttons inside the `widget#73` group are disabled

### Scenario: same issue number in two repositories (R5)

- **Given** the board's Queued column holds ghosts `alpha#73` and `beta#73`, both expanded
- **When** the operator looks for the Boost button of `beta#73`
- **Then** `within(getByRole('group', { name: 'beta#73' })).getByRole('button', { name: 'Boost' })`
  finds exactly one button, distinct from the one inside the `alpha#73` group
- **And** no group is named by the number alone: `queryByRole('group', { name: '#73' })` and
  `queryByRole('group', { name: '73' })` return null

### Scenario: boundary — the group name is exactly the identifier (R1)

- **Given** the expanded `widget#73` ghost
- **Then** `getByRole('group', { name: 'widget#73' })` matches by exact string, and no group is
  named `Details for widget#73` or `Actions for widget#73`

### Scenario: board-level disambiguation with several ghosts (R1, R5)

- **Given** the board shows two expanded ghosts `widget#73` and `widget#74`
- **When** the test queries `getAllByRole('group')` within the Queued column
- **Then** the groups are named `widget#73` and `widget#74`, each holding its own four buttons

### Scenario: no visual change (R6)

- **Given** the expanded `widget#73` ghost
- **Then** the group element carries the classes `mt-2 flex gap-1`, as the unnamed row did

## What moved in the code since the issue was filed

- The issue says "`{target}#{number}`". The ghost type has no `target` field; the code calls it
  `repository` (`GhostCard.repository`), and `CompactCard` already renders the identifier as
  `${repository}#${number}`. The group name uses that same string, so the header, the expand
  toggle (`Details for widget#73`) and the group all name the issue identically.
- The action buttons now live behind expand in `CompactCard`'s detail block, so the group only
  exists while a card is expanded; the scenarios expand first.
- `BoardPage` keys ghosts by `repository#number` because numbers collide across projects; R5
  carries that constraint into the group name.

- The queue actions themselves are keyed by issue number only: `onBoost`/`onNext`/`onReady`
  receive the number, and `POST /api/queue/boost` sends `{ issue, amount }`. The group names the
  issue for assistive technology; it does not change which issue an action reaches (R4).

## Out of scope

- Renaming or relabelling the four buttons.
- FailureList Retry buttons and their shared `busy` flag (issue #43).
- The task-page ghost view, the queue actions, the API, and card layout.
