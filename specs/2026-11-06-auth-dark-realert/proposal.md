# auth-dark re-alerts during a long outage

## Why

`_auth_dark_edge` (`dispatcher/main.py`, PR #73) sends exactly one `auth_dark` Telegram alert per
incident: once the `auth-dark` marker is 30 minutes old it alerts, writes `"alerted": true`, and
returns early on every later pass until usage is readable again. While the box is dark the
dispatcher fail-safes and spawns nothing, so the outage only ends when a human re-logs in. One
missed, muted or scrolled-past message therefore leaves the box silent for the whole incident —
the outage that motivated the auth-resilience work lasted ~21 hours.

## For whom

The operator, who reads Telegram and is the only one who can re-login on the box.

## Goal

While the box stays dark, the operator is reminded with a fresh `auth_dark` alert every 4 hours
after the first one, keeping the 30-minute grace before the first alert and the marker cleared on
recovery.

## Non-goals

- Making the re-alert interval or the grace configurable in `targets.yaml` or the environment:
  both are module constants, like `AUTH_DARK_GRACE_MINUTES` today.
- Changing the `auth_dark` template text, or adding a "recovered" message.
- Escalation (other channels, growing/shrinking intervals), quiet hours, acknowledging an alert.
- Re-alerting `budget_stall` or any other edge-triggered alert.
- Changing what counts as dark (every fetched provider `unavailable`, or none fetched).
