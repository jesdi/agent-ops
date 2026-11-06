# GhostCard action group carries the issue identifier — Design

## Decisions

- **Name the row, not the buttons** — the action row becomes `role="group"` named
  `{repository}#{number}`; the button names stay `Boost`/`Demote`/`Next`/`Ready`, so every
  existing `getByRole('button', { name: 'Boost' })` query keeps working. Tradeoff: a screen
  reader announces the issue when entering the group, not on every button.
- **The name is a plain `aria-label` string, built like `CompactCard`'s identifier** — the
  same `repository#number` text as the card header and the `Details for …` toggle. Tradeoff: the
  string is built in two places (GhostCard and CompactCard) instead of being passed down; it is one
  template literal, so no shared helper.
- **Keep the existing row element and its classes** — the `div` that already holds the buttons
  gains the role and name; no wrapper element. Tradeoff: none; no layout change (R6).
- **Group lives in the expand-only detail block** — unchanged placement, so the group exists only
  while the card is expanded (R4).
- **No change to queue actions** — handlers still receive the issue number only and
  `POST /api/queue/*` stays `{ issue, ... }` (R4, non-goals). The group fixes the announcement, not
  the routing of an action to a repository.
- **One ticket** — the change is one element in one component; splitting it would leave a ticket
  with no behaviour.

## Data model

None. No table, field or API shape changes. (No `red-team-data-model` run: the skill is not
installed on this box, and the change has no data model to red-team.)

| Table | Field | Constraint | Why |
|---|---|---|---|
| — | — | — | — |

## Identities

None. The change widens no key; the group name reuses the existing `repository#number`
identifier that `BoardPage` already keys ghosts by.

## Seams

- `GhostCardView` rendered with `renderWithProviders` (existing) — the component seam for R1–R4,
  R6 and the single-repository scenarios.
- `BoardPage` rendered with `renderWithProviders` and the `/api/board` MSW handler (existing) — the
  board seam for the two-ghost and same-number-across-repositories scenarios (R1, R5).
