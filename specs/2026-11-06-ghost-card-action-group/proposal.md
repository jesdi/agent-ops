# GhostCard action group carries the issue identifier

## Why

`GhostCardView` (`frontend/src/components/GhostCard.tsx`) renders four ranking buttons per
expanded card — Boost, Demote, Next, Ready — whose accessible names are only those words. With
several ghosts expanded in the Queued column, a screen-reader user hears "Boost" once per card
and cannot tell which issue each button acts on. The real task card already does better: its
expand toggle and link carry the `repository#number` identifier (`Details for widget#73`).
Renaming the buttons (`aria-label="Boost #73"`) would break the `getByRole('button', { name:
'Boost' })` queries the board and component tests rely on, which is why issue #84 was split off
as a follow-up rather than fixed in place.

## For whom

The operator using the board with a screen reader or other assistive technology.

## Goal

Each ghost card's action row is a named group, `role="group"` with the accessible name
`{repository}#{number}`, so every Boost/Demote/Next/Ready button is announced within the context
of its issue, while the button names themselves stay exactly Boost, Demote, Next and Ready.

## Non-goals

- Renaming the four buttons or adding `aria-label`s to them (the issue's settled resolution
  keeps the names).
- The FailureList Retry buttons and their shared `busy` flag (issue #43): a separate issue with
  a behavioural change of its own; the issue thread only suggests doing them "together".
- The single-ghost task page view (`ghost-task-view` in `TaskPage`): one issue per page, so its
  buttons are not ambiguous.
- Any change to the actions, the `busy` disabling, the API, or the card's visual layout.
