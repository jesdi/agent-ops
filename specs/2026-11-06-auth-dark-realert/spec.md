# auth-dark re-alerts during a long outage — Spec

Terms:

- **Dark**: the pass's usage fetch returned no providers, or every provider's `source` is
  `unavailable` (unchanged from today's `_auth_dark_edge`).
- **The marker**: the JSON file `<state_dir>/auth-dark`. Today it holds
  `{"since": <ISO-8601 UTC>, "alerted": <bool>}`. `since` is when the incident began.
- **Grace**: `AUTH_DARK_GRACE_MINUTES` (30), unchanged.
- **Re-alert interval**: a new module constant in `dispatcher/main.py`,
  `AUTH_DARK_REALERT_HOURS = 4`, beside the grace constant. Chosen so a ~21-hour outage sends
  about six alerts (at 0.5 h, 4.5 h, 8.5 h, 12.5 h, 16.5 h, 20.5 h) rather than one or dozens.
- **Last alert time**: the moment the most recent `auth_dark` alert of the current incident was
  sent. The marker gains a field holding it as an ISO-8601 UTC string (the implementation names
  it; the marker keeps `since` and `alerted`).
- **Legacy marker**: a marker written by the current code with `"alerted": true` and no last
  alert time — what a box mid-incident holds when this change deploys.

All behavior is observed through `main.run_pass` with the usage fetch patched, as the existing
auth-dark tests in `tests/test_main.py` do: alerts are counted in `deps.notifier.sent`, the
marker is read from `state_dir`, and elapsed time is simulated by writing back-dated timestamps
into the marker.

## Requirements

1. **Grace is unchanged.** The first dark pass writes the marker with `alerted` false and sends
   nothing; dark passes inside the 30-minute grace send nothing and leave `since` untouched.
2. **First alert is unchanged.** The first dark pass at or past the grace sends exactly one
   `auth_dark` alert, sets `alerted` true and records the last alert time as that pass's time.
3. **Re-alert after the interval.** A dark pass whose last alert time is at least
   `AUTH_DARK_REALERT_HOURS` (4 h) ago sends exactly one more `auth_dark` alert and moves the last
   alert time to that pass's time. `since` and `alerted` stay as they were.
4. **Silent between alerts.** A dark pass whose last alert time is less than 4 h ago sends
   nothing and leaves the marker unchanged.
5. **Alert reports the whole outage.** Every `auth_dark` alert, first or repeated, carries
   `minutes` = whole minutes since `since` (not since the last alert), and the host name, as
   today.
6. **Recovery clears everything.** A pass that is not dark deletes the marker and sends no
   `auth_dark` alert; a later dark incident starts over at requirement 1 (grace, then first
   alert), with no memory of the previous incident's last alert time.
7. **Legacy marker is honored.** A legacy marker counts its last alert time as `since`: it
   re-alerts on the first dark pass once `since` is at least 4 h old, keeps that `since`, and
   records the new last alert time; when `since` is younger than 4 h it stays silent.
8. **Corrupt markers still repair.** A marker that is not JSON, is JSON but not an object, lacks
   `since`, or has an unparseable `since` or last alert time is reset to a fresh marker
   (`alerted` false, new `since`, no last alert time) without raising and without alerting, as
   today.

## Scenarios

### Scenario: first dark pass marks, does not alert (req 1)

- **Given** no `auth-dark` marker and usage `{"anthropic": source=unavailable}`
- **When** `run_pass` runs once
- **Then** the marker exists with `alerted` false and `auth_dark` is not in `notifier.sent`

### Scenario: inside grace stays silent (req 1)

- **Given** a marker written by a dark pass moments ago (`since` = now, `alerted` false)
- **When** a second dark pass runs
- **Then** nothing is sent and `since` is byte-identical to before

### Scenario: first alert at grace boundary (req 2)

- **Given** a marker `{"since": now − 31 min, "alerted": false}`
- **When** a dark pass runs
- **Then** `notifier.sent.count("auth_dark") == 1`, `alerted` is true, `since` is unchanged, and
  the last alert time is within a few seconds of now

### Scenario: one alert, then silence for the next pass (req 2, req 4)

- **Given** a marker `{"since": now − 31 min, "alerted": false}`
- **When** two dark passes run back to back
- **Then** exactly one `auth_dark` alert was sent

### Scenario: re-alert once the interval has passed (req 3)

- **Given** a marker with `since` = now − 5 h, `alerted` true, last alert time = now − 4 h − 1 min
- **When** a dark pass runs
- **Then** exactly one `auth_dark` alert is sent, the last alert time moves to ≈ now, `since`
  stays now − 5 h and `alerted` stays true

### Scenario: re-alert exactly at the boundary (req 3)

- **Given** a marker with `alerted` true and last alert time = exactly now − 4 h (computed before
  the pass, so at pass time it is ≥ 4 h)
- **When** a dark pass runs
- **Then** one `auth_dark` alert is sent

### Scenario: silent just inside the interval (req 4)

- **Given** a marker with `since` = now − 4 h, `alerted` true, last alert time = now − 3 h 59 min
- **When** a dark pass runs
- **Then** no `auth_dark` alert is sent and the marker's contents are unchanged

### Scenario: re-alert does not repeat on the next pass (req 3, req 4)

- **Given** a marker whose last alert time is now − 4 h − 1 min
- **When** two dark passes run back to back
- **Then** exactly one `auth_dark` alert was sent across both passes

### Scenario: a long outage keeps reminding (req 3)

- **Given** a marker with `since` = now − 21 h, `alerted` true, last alert time = now − 4 h − 1 min
- **When** a dark pass runs, the last alert time is then back-dated again to now − 4 h − 1 min,
  and another dark pass runs
- **Then** two `auth_dark` alerts were sent in total — the reminder is not a one-shot

### Scenario: re-alert reports minutes since the incident began (req 5)

- **Given** a marker with `since` = now − 300 min, `alerted` true, last alert time = now − 241 min
- **When** a dark pass runs
- **Then** the `auth_dark` alert's `minutes` is 300 (± 1 for pass timing), not 241 and not 0,
  and `host` is `socket.gethostname()`

### Scenario: recovery clears the marker (req 6)

- **Given** a marker with `alerted` true and last alert time = now − 1 h
- **When** a pass runs with usage readable again (`source` oauth, util 0.2)
- **Then** the marker is gone and no `auth_dark` alert was sent by that pass

### Scenario: a new incident after recovery starts with grace (req 6)

- **Given** an incident that alerted and then recovered (marker deleted)
- **When** usage goes dark again and one dark pass runs
- **Then** a fresh marker exists with `alerted` false, no last alert time, and no new
  `auth_dark` alert was sent

### Scenario: legacy marker past the interval re-alerts (req 7)

- **Given** a legacy marker `{"since": now − 5 h, "alerted": true}` (no last alert time)
- **When** a dark pass runs
- **Then** exactly one `auth_dark` alert is sent, `since` is still now − 5 h, and the marker now
  holds a last alert time ≈ now

### Scenario: legacy marker inside the interval stays silent (req 7)

- **Given** a legacy marker `{"since": now − 2 h, "alerted": true}`
- **When** a dark pass runs
- **Then** no `auth_dark` alert is sent and `since` is unchanged

### Scenario: non-JSON marker repairs (req 8)

- **Given** the marker file contains `not valid json{{`
- **When** a dark pass runs
- **Then** it does not raise, the marker is valid JSON with `alerted` false, and nothing is sent

### Scenario: JSON `null` marker repairs (req 8)

- **Given** the marker file contains `null`
- **When** a dark pass runs
- **Then** it does not raise, the marker has `alerted` false, and nothing is sent

### Scenario: unparseable last alert time repairs (req 8)

- **Given** a marker `{"since": now − 5 h, "alerted": true, <last alert field>: "garbage"}`
- **When** a dark pass runs
- **Then** it does not raise, the marker is reset (`alerted` false, fresh `since`), and nothing
  is sent

### Scenario: live usage never touches the marker (unchanged guard)

- **Given** usage with `source` oauth and util 0.95 (full window, readable)
- **When** a pass runs
- **Then** no marker exists and no `auth_dark` alert is sent

## What moved in the code

Nothing beneath the issue's proposal: `_auth_dark_edge` (`dispatcher/main.py`) and its tests
(`tests/test_main.py`, "auth-dark edge tests") are as PR #73 left them, the `auth_dark`
template (`telegram/templates.py`) already renders `{minutes}` and `{host}` only, and
`_budget_edge` still defers the unavailable case to this edge. The existing test
`test_auth_dark_alerts_once_after_grace` keeps passing as written: its third pass runs seconds
after the first alert, inside the interval.

## Out of scope

- A configurable interval or grace; template changes; a recovery message.
- Escalation, quiet hours, acknowledgement; re-alerting other edges such as `budget_stall`.
