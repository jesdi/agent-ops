# Board triage layout: zones, compact cards, mobile tabs

## Problem Statement

The console board renders eleven columns side by side, in pipeline order,
whether or not they hold anything. On a normal day two or three of them
matter and the rest are 256px of empty space. To find the columns that need
a human (a spec to review, a PR to merge, a parked task, a failure) the
operator scans the whole row every time.

The row is one horizontally scrolling flex container that grows as tall as
its longest column. Its scrollbar therefore sits below the fold, so moving
right on a desktop means scrolling to the bottom first.

Every card shows every fact it has: target, issue, title, stage, model,
track, score, slot, park reason, notify state, feedback state, mail count,
wake state, claimed time, updated time. Cards are tall, columns are long, and
the board never fits on a screen. Most of that detail is only wanted when
looking into one task.

There is no phone layout at all. The board on a 390px viewport is the desktop
row, scrolled.

## Solution

The board splits into two zones. A **Needs you** zone comes first with the
columns a human has to act on: Needs review, PR review, Parked, Failed,
Stalled on budget. A **Pipeline** zone follows in flow order: Queued, In
progress, Awaiting CI, Resuming, Done, Wont do. Zone and order come from the
read model, so every consumer sees the same grouping.

Empty columns leave the row and appear as greyed count chips in a strip above
the board, so the absence of failures is a visible zero rather than a missing
column. The Wont do chip still accepts a dragged card.

The board fills the viewport below the header. Each column scrolls
vertically under a sticky header; the row scrolls horizontally, and its
scrollbar is always at the bottom of the screen. Most days it does not need
to scroll at all.

Cards are compact by default: identifier, title, one signal line, and a
right-aligned updated stamp. Signals are only the things that need action or
explain why the card is in its column. Model, track, score, slot chip,
claimed time and cycle time appear when the card is expanded. Expansion is
transient. Ghost cards in Queued get the same treatment, with their four
ranking actions behind the expand.

On phones the board shows one column at a time. A scrollable tab row lists
the occupied columns with counts, Needs you first, and the count strip sits
under it. The header collapses to one summary line with the full row a tap
away.

The whole app gets a small token system: one typeface, a cool near-white
surface, slate ink, four status hues (green running, amber waiting on you,
red failed, violet parked), and dark mode from the OS setting. The Needs you
zone has a visibly different surface from the pipeline. Shell and board are
restyled fully; task page, failures and history inherit tokens only.

## User Stories

1. As an operator, I want the columns that need me first, so that I find my work without scanning the whole row.
2. As an operator, I want PR review treated as a column that needs me, so that PRs waiting for my merge are not buried in the pipeline.
3. As an operator, I want the Needs you zone to look different from the pipeline, so that I see the split rather than infer it from order.
4. As an operator, I want empty columns out of the row, so that the board fits on my screen on a normal day.
5. As an operator, I want empty columns still named as zero-count chips, so that "no failures" is visible and not just absent.
6. As an operator, I want an occupied column never to move into the chip strip, so that a card can never disappear from view.
7. As an operator, I want to drag a card onto the Wont do chip when that column is empty, so that cancelling a task works the same on quiet days.
8. As an operator, I want the horizontal scrollbar at the bottom of the screen, so that I never scroll down to move right.
9. As an operator, I want each column to scroll on its own with its header pinned, so that a long Queued column does not push the others off screen.
10. As an operator, I want column positions to stay stable as columns empty and fill, so that my eye learns where things are.
11. As an operator, I want cards to show two lines by default, so that I can see a whole column at once.
12. As an operator, I want the pending-intent badge, admission warning, mail count, wake-blocked, feedback-queued and notify-pending signals visible on the compact card, so that I never miss something that needs action.
13. As an operator, I want the park reason on compact Parked cards, so that the column answers "why is this parked" without expanding.
14. As an operator, I want the stage label on compact In progress cards, so that I can tell spec from implement from review at a glance.
15. As an operator, I want a relative updated stamp on every compact card, so that I can spot a stuck task by its age.
16. As an operator, I want the E2E slot colour kept as the card's left border, so that slot ownership survives compaction.
17. As an operator, I want to expand one card to see model, track, score, slot, claimed and cycle time, so that detail is there when I look for it.
18. As an operator, I want expansion to reset on reload, so that the board always opens compact.
19. As an operator, I want the card title to open the task page and a chevron to expand, so that the two actions never collide.
20. As an operator, I want the whole card to remain draggable, so that drag-to-cancel keeps working.
21. As an operator on a phone, I want tapping the card body to expand it rather than navigate, so that I can inspect without leaving the board.
22. As an operator, I want ghost cards compact with the Next badge visible, so that the queue reads like the rest of the board.
23. As an operator, I want Boost, Demote, Next and Ready on an expanded ghost, so that ranking is still done from the board.
24. As an operator, I want the stale-queue marker and queue action error to stay in the Queued header, so that degraded state is never hidden by density.
25. As an operator on a phone, I want one column at a time with a tab per occupied column, so that cards are readable at full width.
26. As an operator on a phone, I want tab counts, so that the tab row is the overview I lack on desktop.
27. As an operator on a phone, I want the selected column in the URL, so that back and reload return me to the same tab.
28. As an operator on a phone, I want the header collapsed to capacity and next-claim verdict, so that the board starts above the fold.
29. As an operator on a phone, I want the full header on tap, so that usage and cycle time are still reachable.
30. As an operator, I want dark mode from my OS setting, so that the console matches the rest of my screen at night.
31. As an operator, I want colour to mean state and nothing else, so that a coloured element is always worth reading.
32. As an operator, I want the task page, failures and history to use the same palette and typeface, so that the app reads as one product.
33. As an operator, I want keyboard focus visible and reduced motion respected, so that the board is usable without a mouse or with motion sensitivity.
34. As a maintainer, I want zone and column order in one backend place, so that the frontend and any other consumer cannot drift.
35. As a maintainer, I want the session-only collapse store deleted, so that no state exists that the data does not already imply.

## Implementation Decisions

- **Zones live in the read model.** The column tuple gains a zone per column and is reordered to Needs you (Needs review, PR review, Parked, Failed, Stalled on budget) then Pipeline (Queued, In progress, Awaiting CI, Resuming, Done, Wont do). The `Column` schema gains a `zone` field with two values. Column keys do not change. The frontend renders zones in the order received and holds no column ordering of its own.
- **OpenAPI types are regenerated** from the read model with the existing generator; the frontend consumes the `zone` field through the generated `Column` type.
- **The count strip is derived, not stored.** A column with no cards and no ghosts renders as a chip; a column with either renders in the row. The zustand collapse store and its toggle are removed. No board layout state is persisted anywhere.
- **The Wont do chip is a drop target** using the same drag payload and confirm dialog as the column.
- **Desktop layout** is a viewport-height board: header row, count strip, then a row of columns with horizontal overflow. Each column is a flex column with a sticky header and an independently scrolling body. The page itself does not scroll on desktop.
- **Mobile layout** at widths below the `md` breakpoint: tab row of occupied columns with counts, count strip, one column body at full width. The active column key is a URL search parameter; with none set the first occupied column is active. The same column component renders in both layouts.
- **Header on mobile** shows capacity fraction and the next-claim detail on one line; a disclosure reveals the full header row. Desktop keeps the current row.
- **Compact card contract.** Line one: `target#issue`, pending badges, updated stamp right-aligned. Line two: title, which is the link to the task page. Signal line, rendered only if non-empty: admission warning, mail count, wake blocked, feedback queued, notify pending, park reason (Parked), stage label (In progress). Left border keeps the slot colour. A chevron toggles an expanded block with model, track, score, slot chip, claimed time and cycle time. Expanded state is component-local.
- **Ghost cards** share the compact shape: identifier, title link, Next badge; expanded block shows score, boost, admission warning and the four ranking buttons. Busy handling is unchanged.
- **Card body tap on touch** expands instead of navigating; the title link still navigates. The article stays the drag source.
- **Tokens** are CSS custom properties in the single existing stylesheet, mapped into Tailwind v4 theme variables: surface, surface-raised, ink, ink-muted, border, and the four status hues each with a background and foreground. Dark values under the OS media query. One typeface, IBM Plex Sans with tabular figures, loaded from Google Fonts with a system-sans fallback so the box works offline.
- **Zone surface.** The Needs you zone gets a faint warm surface tint and a heavier zone header; the pipeline zone stays on the base surface. This is the one deliberate visual emphasis; no other decoration is added.
- **Restyle scope.** Shell nav, board page and every board component are restyled to tokens. Task page, failures and history only swap hard-coded greys and status colours for token classes; their layouts are untouched.
- **Accessibility floor.** Visible focus rings on every interactive element, `prefers-reduced-motion` disables the pending-badge pulse and any transition, expand toggles carry `aria-expanded`, tab row uses tab semantics, the count strip is a list with each chip naming its column and count.

## Testing Decisions

A good test drives the board through its public surface, the rendered DOM and the HTTP API, and asserts what an operator would see. No test reads component state or the store.

- **Read model (pytest).** The board snapshot returns columns in the new order with the right zone on each; column-for-stage and column-for-park mappings are unchanged. Prior art: existing read-model tests in the web test suite.
- **Board page (vitest with MSW).** Given a board with some empty columns: occupied columns render in the row in zone order, empty ones render as chips, a card is never both. Compact card shows identifier, title, updated stamp and only the applicable signals; expanding reveals the detail block; ghost actions appear only when expanded. Queued header still shows stale and error markers. Prior art: the existing BoardPage, BoardColumn, TaskCard and GhostCard tests, which are updated rather than added beside.
- **End-to-end (Playwright).** A second project for a Pixel 7 viewport alongside Desktop Chrome. A board spec covers: chips for empty columns on both, per-column scroll and no page scroll on desktop, tab row with counts and URL-carried selection on mobile, expand and collapse on both, drop onto the Wont do chip. Existing queue, park, message and artifacts flows run under both projects unchanged. Prior art: the existing board-loading spec already iterates two viewports.
- **Visual review.** Playwright screenshots at both widths in light and dark, reviewed by hand before the PR.

## Out of Scope

- Redesign of the task page, failures page or history page beyond token inheritance.
- Operator-configurable column order, pinning, or a board-wide density toggle.
- Persisting any board layout state.
- Changing which stage or park maps to which column.
- Telegram or any other consumer adopting zones, beyond the field being available.
- Any change to queue, park, cancel or force-run behaviour.

## Further Notes

- Order inside Needs you is action order: a review is a read, a PR is a merge, a park is usually a short reply, a failure is an investigation, and a budget stall is usually a wait.
- One PR. The backend change is a tuple reorder plus one field; splitting it would only add a coordination step.
- Delivery goes through the no-mistakes pipeline after a rebase onto origin/main.
