# 0008. Anthropic usage has no local-log fallback

Date: 2026-11-10

## Context

`ccusage` reads local session logs, which do not establish whether the OAuth
token is live. Returning its session window after every OAuth token fails
hides unavailable usage from the auth-dark detector and the spawn gate.

## Decision

Delete the ccusage fallback instead of retaining it behind a flag. The
Anthropic adapter reads OAuth endpoint → unavailable: when every candidate
token is rejected or yields no windows, it returns an unavailable reading.
No flag may turn local-log readings back into a substitute for OAuth usage.

## Consequences

An OAuth outage also produces unavailable usage. The auth-dark detector and
spawn gate consume that result through the existing adapter boundary.
The legacy source literal and frontend label remain for compatibility;
their presence does not authorize producing new ccusage readings. Old cached
readings expire through the existing minimum polling interval.
