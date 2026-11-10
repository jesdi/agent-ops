# 0013. Auth-dark alerting: the marker owns the state, reminders every 8 hours

Date: 2026-11-10

## Context

When usage goes dark (every provider `unavailable`) the box fails closed and
needs a human. One `auth_dark` alert after a 30-minute grace was easy to miss
in an outage that lasts a day, so the alert now repeats (issue #77).

## Decision

- The `<state_dir>/auth-dark` marker is the only alert state. It gains
  `last_alert` beside `since` and `alerted`; there is no second file to keep
  in step, and recovery deleting the marker forgets the whole incident.
- Cadence is code policy: `AUTH_DARK_GRACE_MINUTES` (30) and
  `AUTH_DARK_REALERT_HOURS` (8) are constants in `dispatcher/main.py`, not
  settings. Changing them is a code change with tests.
- The interval runs from the last successful send, not from `since`. A late
  pass delays only that reminder and never pulls the next one earlier.
- Every alert's `minutes` is the age of `since`, so the latest message alone
  tells the reader how long the box has been dark.
- Send first, then write the marker. Telegram and the file cannot be written
  atomically together. A failed send leaves the marker unchanged so the next
  pass retries. A failed write after a send repeats one alert, which is the
  cheaper failure.

## Consequences

- Edge rulings live in the spec (`specs/2026-11-06-auth-dark-realert/spec.md`,
  reqs 7 and 8) and `tests/test_main.py`: a legacy marker (alerted, no
  `last_alert`) counts from `since`; alerted markers are parsed and repaired;
  a corrupt `last_alert` resets the incident like a corrupt `since`; an
  unalerted marker ignores any `last_alert`. A `last_alert` in the future
  (clock stepped back) gets no special case and only delays the next reminder.
