# Auth resilience: no more silent OAuth death

**Date:** 2026-07-31
**Status:** Approved

## Problem

On 2026-07-30 the box froze for ~21 hours with no operator-visible signal.
Four links compounded:

1. The keepalive refreshes the claude-home OAuth token **once a day**
   (midnight). When the refresh failed ("OAuth session expired and could not
   be refreshed"), the session was already past recovery.
2. The keepalive failure was **silent** — an exit-code 1 in the user journal.
3. The dispatcher correctly fails safe when usage is unknowable, but that
   state is **indistinguishable from a quiet night** unless someone reads
   `usage-cache.json` mtimes.
4. The eventual manual re-login defaulted to `~/.claude`, not the
   claude-home store the fleet actually reads (`CLAUDE_CONFIG_DIR` trap),
   requiring a second intervention.

A browser OAuth login can never be automated, so the goal is: make token
death near-impossible, and when it happens anyway, alert immediately with a
one-command fix. (Approach chosen over 1Password sync-back — rebuild-day
benefit only — and over a ccusage usage fallback, which would hide the real
problem: containers still can't spawn with a dead token.)

## Design

### 1. Hourly keepalive

`provision/agent-ops-keepalive.timer`: `OnCalendar=daily` →
`OnCalendar=hourly`. `Persistent=true` stays. The service is unchanged
(one `claude -p --permission-mode plan "Reply with exactly: ok."` against
claude-home). Exercising the refresh token hourly keeps it inside its
refresh window, and a failure is detected within the hour instead of up to
24 h later.

### 2. Keepalive failure → Telegram

- New template unit `provision/agent-ops-alert@.service`. Instantiated with
  the failing unit's name (`%i`), it runs
  `python3 -m telegram.alert %i` from the repo checkout, with the same
  `EnvironmentFile` the dispatcher uses for `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID`.
- `agent-ops-keepalive.service` gains `OnFailure=agent-ops-alert@%n.service`.
- New module `telegram/alert.py`: thin `main(unit)` that calls
  `Notifier.send("unit-failed", unit=unit)`.
- New template `unit-failed` in `telegram/templates.py`. The message names
  the failed unit and includes the literal recovery command, with the host
  filled in from `socket.gethostname()` at send time:

  ```
  ssh -t agent@$(hostname) 'CLAUDE_CONFIG_DIR=$HOME/agent-ops-state/claude-home \
    $HOME/.local/bin/claude' → /login
  ```

- Best-effort end to end: `Notifier` already degrades to stderr; an alert
  failure never propagates back into the unit chain.

### 3. Dispatcher "running dark" alert

In the dispatcher pass, when `fetch_usage` returns `source ==
"unavailable"` (usage unknowable → fail-safe, no spawns):

- First such pass writes an `auth-dark` marker file (JSON: `since`,
  `alerted`) in the state dir, next to `budget-stalled`.
- A later pass that finds the marker with `since` older than **30 minutes**
  and `alerted: false` sends one Telegram alert (new `auth-dark` template,
  same recovery command) and rewrites the marker with `alerted: true` —
  one alert per incident, mirroring the `budget-stalled` pattern.
- The first pass where usage resolves again (any source other than
  `unavailable`) deletes the marker.

The 30-minute grace absorbs transient API blips. A full budget window
(`utilization ≥ threshold` with a live source) is expected behavior and
never alerts.

### 4. Credential-store convergence

Two halves; either alone fixes the wrong-store trap, together it becomes
unhittable:

- **`provision/update.sh`** gains a convergence step: if
  `~/.claude/.credentials.json` exists, parses as JSON with a non-empty
  `claudeAiOauth.accessToken`, and is strictly newer (mtime) than
  claude-home's copy, `install -m 600` it into
  `$STATE_DIR/claude-home/.credentials.json`. A login into the default
  store self-heals within one updater pass (~1 min). Corrupt or tokenless
  files are never copied.
- **`provision/bootstrap.sh`** idempotently appends
  `export CLAUDE_CONFIG_DIR="$HOME/agent-ops-state/claude-home"` to the
  agent user's `~/.profile` (grep-guard before append), so future
  interactive logins write to claude-home directly. Systemd units are
  unaffected — each already sets its own environment.

## Not doing

- 1Password sync-back of refreshed credentials (rebuild-day-only benefit;
  op service-account write plumbing not worth it now).
- ccusage as a usage-reading fallback (masks dead auth instead of fixing it).
- Any change to fail-safe semantics: usage unknown still means no spawns.

## Testing

- **Marker lifecycle** (unit, alongside existing budget/dispatcher tests):
  unavailable usage creates the marker; alert fires exactly once after the
  grace period; recovery deletes the marker; a full-but-live budget window
  never touches it.
- **`tests/test_update_script.py`**: convergence step copies a newer valid
  credentials file; skips older, missing, corrupt-JSON, and
  tokenless files; result is mode 600.
- **Template render** tests for `unit-failed` and `auth-dark` (recovery
  command present).
- **Bootstrap idempotence**: `.profile` gains the export once across two
  runs.

## Rollout

Seed-converged like everything else: merge to main, the box's updater pulls,
`systemctl --user daemon-reload` + timer re-enable happen via the existing
unit-convergence path. No manual box steps.
