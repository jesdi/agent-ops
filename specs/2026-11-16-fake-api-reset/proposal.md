# One reset for all fake API state

## Why

The Playwright fake API shares mutable state across serial tests. Its two reset
handlers restore disjoint subsets: the queue reset replaces ghosts, priority and
next-claim data and filters pinned cards, but never restores the parked task or
capacity after `free-slot`. The message reset restores those columns, messages
and intents but leaves boosts, queue order, next-claim and priority behind.
Duplicated startup and reset capacity literals also disagree (`slots_used` is 1
at startup and 0 after the message reset). Thus the next test inherits state
from earlier tests even after resetting. On the current checkout, the committed
HTTP end-to-end reproduction fails twice: after moving task #42 and resetting
the queue, parked cards are `[]` instead of containing #42. Changing only the
reset to the message reset restores #42, confirming omitted restoration rather
than a damaged seed or an asynchronous UI race. Issue #90 defines the scope.

## For whom

Contributors and CI running the console's Playwright end-to-end suite.

## Goal

One POST to `/__control__/reset` restores every mutable fake API domain to a
shared startup seed, with zero occupied slots and a newly computed next-pass
ETA. Existing tests use that reset and remain isolated across repetitions and
file ordering.

## Non-goals

No parallel workers, per-worker fake servers, production backend changes, UI
redesign, or edits to stale `.lavish/` notes. Do not change the behavior of
`seed-pinned`, `apply-intents`, `free-slot`, or any `/api` contract. This stage
contains only the failing reproduction and stage 1 documents; implementation,
design and tickets belong to later stages.
