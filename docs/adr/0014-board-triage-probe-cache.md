# 0014. The board caches only the triage session probe

Date: 2026-11-13

## Context

`GET /api/board` calls `Sources.triage_state()`. The herdr session probe can
take up to two 30-second subprocess timeouts, while the triage request file is
a local read that must be reflected on the next board load.

## Decision

- Cache only `triage.running()` per `Sources` instance. Read the triage request
  file on every `triage_state()` call, including when the cached probe says a
  triage session is running.
- Keep one `(timestamp, bool)` cache attribute, empty until the first probe.
  Do not add a lock: two simultaneous cold requests may each probe. The
  operator accepted this at the plan gate to avoid serializing callers behind
  a stalled probe.
- Cache a raising probe as `False`, timestamped when the probe finishes. This
  bounds repeated slow failures without asserting that claims are paused.
- Use the injected `Sources` clock and a strict `<` comparison against
  `TRIAGE_TTL_SECONDS = 5.0`, a module constant. At the exact boundary, the
  next call probes again.

## Consequences

- A failed probe can hide a real triage session for up to five seconds. The
  request file remains visible on the next board load.
- Concurrent cold requests can duplicate the probe. Future changes to that
  behavior need to account for the cost of waiting behind a stalled herdr call.
