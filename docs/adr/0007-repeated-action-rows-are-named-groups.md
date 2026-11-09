# 0007. Repeated action rows are named groups, not renamed buttons

Date: 2026-11-08

## Context

The board repeats the same action buttons on every card (a ghost's Boost,
Demote, Next and Ready). Their accessible names are only those words, so a
screen-reader user moving across several expanded cards cannot tell which
issue a button acts on. Renaming each button (`aria-label="Boost #73"`) would
break every `getByRole('button', { name: 'Boost' })` query the tests rely on
(issue #84).

## Decision

- A row of per-issue actions is an element with `role="group"` whose
  accessible name is the issue identifier `{repository}#{number}`, the same
  string the card header and its `Details for …` toggle use. The button names
  stay the bare action words.
- The name always includes the repository: issue numbers collide across a
  project's repositories (`alpha#73`, `beta#73`).
- The group is the existing row element with its classes; no wrapper and no
  layout change.

## Consequences

- A screen reader announces the issue on entering the group, not on every
  button.
- Tests find an action of one card with
  `within(getByRole('group', { name: 'widget#73' })).getByRole('button', { name: 'Boost' })`;
  bare button queries keep working where only one card is in scope.
- The next repeated action row (for example the FailureList Retry buttons,
  issue #43) follows the same pattern instead of renaming its buttons.
- The group names the issue for assistive technology only; it does not change
  which repository a queue action reaches (that is issue #235).
