# Console banner for unreadable usage (auth-dark) and over-pace usage

## Why

When every provider's usage is unreadable, the dispatcher spawns nothing, indefinitely. It
writes an `auth-dark` marker and sends a Telegram alert after 30 minutes (repeated every 8 hours
since #77, ADR 0013). The web console never reads that marker, so the console shows nothing at
the moment it matters most. A window-exhausted pause (`budget-stalled`) has no console banner
either: only a small "will not spawn" chip with the reason on hover, and the next-claim line.
The two cases need different wording, because one resolves by itself and the other needs the
operator to log in again.

Decided by the operator on 2026-11-05 (issue #78): show a visible banner at the top of the Board
page when usage cannot be read (the dispatcher's `auth-dark` marker exists), and a second,
differently worded banner when usage is over pace.

## For whom

The operator, watching the Board page of the web console (desktop or phone).

## Goal

The Board page shows one alert banner above the header while the `auth-dark` marker exists, a
differently worded one while the live usage gate is closed by a binding window, and nothing
otherwise; each banner appears and disappears live through the existing `usage` SSE key.

## Non-goals

- Any change to the dispatcher, the marker format, the 30-minute grace or Telegram alerts.
- Reading `budget-stalled` in the web layer (the exhausted case comes from the live gate instead).
- Changing or removing the per-provider "usage unknown" notice in `UsagePanel`.
- A dismiss button, sound, browser notification, or a banner on pages other than Board.
- A new SSE key or a new endpoint.
- Changing the next-claim line (`auth-dark` / `budget-blocked` verdicts in `lib/nextClaim.ts`).
