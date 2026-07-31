# Capacity visibility on the board

## Problem

When the dispatcher stops claiming new work, the board answers "how full am I"
but not "who is filling me". The header reads `2/3 active · slots 2/3` and
nothing on any card says whether that card is one of the two holding a unit.

The rule is not guessable from the card either. A task consumes capacity when
its stage is in-flight **and** it is unparked — except `parked-login`, which
keeps its container and tmux session running and therefore keeps consuming.
So a card sitting in a park column may or may not be the reason nothing new is
being claimed, and the board gives the operator no way to tell.

This spec makes capacity occupancy visible per card, so "three units in use"
can be resolved to "these three tasks" at a glance.

## Scope

In scope: a per-card visual marker for capacity holders, and a header meter
that reads as occupancy and doubles as the marker's legend.

Out of scope: E2E slots. Slots stay plain text in the header. A second meter
would dilute the one signal this feature exists to make obvious.

## Design

### The predicate gets one home

`consumes_capacity(t: TaskState) -> bool` is extracted in `dispatcher/state.py`
and becomes the single definition of the rule:

```python
def consumes_capacity(t: TaskState) -> bool:
    """One task's answer to "does this hold a capacity unit?" — see active()."""
    return t.stage in IN_FLIGHT_STAGES and (not t.park or t.park == PARK_LOGIN)


def active(tasks: list[TaskState]) -> list[TaskState]:
    return [t for t in tasks if consumes_capacity(t)]
```

`active()` keeps its existing docstring explaining *why* `PARK_LOGIN` counts;
`consumes_capacity` is the per-task form of the same rule, not a second one.

The frontend never re-derives this. The exception is real and a TypeScript copy
would drift the first time it changes.

### The read model carries the flag

`web/read_model.py`:

- `TaskCard` gains `consuming_capacity: bool`.
- `task_card()` sets it from `consumes_capacity(t)`.

`CapacityView` is unchanged — `active` and `capacity` are already everything the
meter draws.

This makes the board self-reconciling: the number of cards carrying the flag
equals `capacity.active`. That is the check the operator performs by eye, so it
is also asserted in a test.

### Card accent

`TaskCardView` renders a left border accent when `card.consuming_capacity` is
true. The card's existing `border-gray-200` stays; only the left edge changes.

```
│▍ portfolio_eval#187                  │  holding a capacity unit
│  Add ranked backlog view             │
│  implement · opus · slot 1           │

│  portfolio_eval#191                  │  in-flight, parked: no accent
│  Fix nightly digest                  │
│  spec · sonnet · parked: awaiting-ci │
```

Colour encodes the board-wide state, not the card's:

- **amber** (`border-l-amber-500`) when `active >= capacity` — full, and
  therefore the explanation for a dispatcher that is not claiming;
- **blue** (`border-l-blue-500`) when there is headroom — busy but fine.

Every accented card shares the colour, so the board flips amber as a whole the
moment capacity saturates.

Colour is never the only signal. The accented card also renders a
visually-hidden span reading `holding a capacity unit`, so the marker survives a
colourblind reader and is asserted in tests as text rather than as a Tailwind
class. A visually-hidden span rather than `aria-label` on the card: the card is
a `Link`, and an `aria-label` there would replace its whole accessible name
(target, issue and title) with the capacity note.

### Header meter

The `{active}/{capacity} active` text becomes a discrete pip meter — one pip per
capacity unit, filled pips in the accent colour, empty pips gray — followed by
the existing text unchanged.

Discrete rather than continuous: capacity is a small integer, and pips let the
operator count holders and match them against accented cards. `BudgetBar` stays
a continuous gauge because utilization genuinely is continuous.

The meter carries `role="progressbar"` with `aria-valuemin`/`aria-valuemax`/
`aria-valuenow`/`aria-valuetext`, matching `BudgetBar`'s existing shape.

`slots {slots_used}/{max_slots}` stays as plain text beside it.

### Components

- `CapacityMeter` — new component in `frontend/src/components/`, takes
  `CapacityView`, renders pips plus the text. `BoardPage` already composes board
  data, budget, pending intents and column-collapse state; pip markup inlined
  there would grow a page that should stay a composition.
- A small exported helper owns the colour decision (`active >= capacity` →
  amber, else blue). The meter and the cards both call it, so they cannot
  disagree about what colour "now" is.

`BoardColumn` is unchanged — it already forwards whole cards.

## Edge cases

- **`capacity <= 0`** (bad `targets.yaml`): the meter falls back to text-only
  rather than rendering an empty pip strip, which would read as "nothing
  running".
- **`active > capacity`** (capacity lowered while tasks are in flight): render
  `capacity` pips, all filled, amber; the text still reads the true `3/2`. The
  text is the truth, the pips are the picture.
- **Done / merged columns** never accent — `consumes_capacity` is false for
  those stages. The `failed` column is an exception: `Stage.BLOCKED` is in
  `IN_FLIGHT_STAGES` and maps to `failed`, so a blocked task correctly renders
  there carrying the accent. This falls out of the predicate; no
  special-casing in the view.

## Testing

**`dispatcher/state.py`**
- Table test over park values: no park → true, `parked-login` → true, every
  other park value → false; non-in-flight stages → false.
- Over a mixed fixture, `active(tasks) == [t for t in tasks if
  consumes_capacity(t)]`, so the extraction cannot silently diverge.

**`web/read_model.py`**
- Reconciliation invariant: for a board built from a mixed task list, the count
  of cards with `consuming_capacity` equals `capacity.active`.

**`TaskCard.test.tsx`**
- Accented when the flag is set, not when it is not; asserts on the accessible
  text, not on class names.

**`CapacityMeter` test**
- Pip counts for `1/3` and `3/3`; the `capacity: 0` text-only fallback; the
  `3/2` overflow; and the colour boundary — `2/3` blue, `3/3` amber.

**`BoardPage.test.tsx`**
- Extend the existing fixture so a `parked-login` card renders accented while an
  `awaiting-ci` card in the same column does not. That is the case that
  motivated the feature.

## Mechanical follow-through

- `frontend/src/test/fixtures.ts` — add `consuming_capacity` to card fixtures.
- `frontend/src/lib/api-types.ts` is generated from the OpenAPI schema; it is
  regenerated, never hand-edited.
