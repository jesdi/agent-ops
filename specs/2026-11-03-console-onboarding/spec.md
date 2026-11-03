# Increment 5: console onboarding

Approved layout: A (worksheet) in local frontend/src/pages/onboarding.prototype.html; reproduce using real console tokens and React components, never commit prototype.

Observable behavior:
- Operator opens Add project from console, enters immutable project slug, existing board owner/number, one or more GitHub repositories and setup/gate/verify commands. Gate required; setup/verify optional.
- Submitting durably queues start and displays pending state until engine creates record. Never report record missing merely because timer has not run.
- Console lists and displays onboardings started by CLI as well as web, including completed ones.
- Detail shows actual engine steps, states, details, links and notice; waiting, failed and blocked clearly distinguished.
- Confirmation shows actual resolved proposal including repositories, commands and board. Confirm explicitly binds the revision shown; polling cannot silently substitute a newly changed revision beneath the review action.
- Resume submits a payload-free intent so it never replays stale request data. Engine is sole record writer and sole owner of transition policy.
- Complete ends paused; link back to board. Infrastructure PR merge is manual; no merge button or credential in web. Abandon, inspection, board creation/import, app auth, secrets and verification expansion deferred.
- CLI remains on box unchanged. Work on issues 252 and 253 belongs to other sessions.
- Preserve existing auth and origin validation. Validate all input, project path especially. Show validation failures inline and preserve edits.
- Every submitted action visibly pending, polled independently of SSE (onboarding has no SSE event).

Agreed test interfaces: authenticated HTTP endpoints + visible React page + browser start/review/confirm/resume flow. Test first in vertical slices, real engine and filesystem for integration, mock subprocess/network only. Final Python tests, API generation, frontend build/tests, CRAP gate, browser verification and full review-diff. PR uses pr and pr-visual-diff skills. No merge without user's instruction. Verify box deploy after merge before calling task done.
