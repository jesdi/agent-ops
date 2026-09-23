import type { NextClaimView } from './api'
import { formatDuration } from './format'

/** Maps each known verdict to a detail string factory.
 *  A lookup keyed by verdict guarantees an unhandled verdict cannot silently
 *  fall through to wrong copy — unknown keys hit the explicit fallback branch. */
const VERDICT_DETAIL: Partial<Record<string, (nc: NextClaimView) => string>> = {
  'will-claim':    (nc) => `will claim #${nc.next_issue}`,
  'budget-blocked':(nc) => nc.blocked_by || `budget resets in ${formatDuration(nc.minutes_to_reset * 60)}`,
  'capacity-full': ()   => 'capacity full — waits for a free slot',
  'no-candidates': ()   => 'queue empty — nothing to claim',
  // claims-paused: the dispatcher pass will still run; only claiming is skipped
  // while a triage sweep is pending. Distinct from errors and from empty queue.
  'claims-paused': ()   => 'claiming paused — triage sweep pending',
}

/** The verdict in a few words, without the countdown: the phone header's
 *  summary line. */
export function nextClaimDetail(nextClaim: NextClaimView): string {
  if (nextClaim.verdict === 'unknown') return 'dispatcher not running?'
  const detailFn = VERDICT_DETAIL[nextClaim.verdict]
  return detailFn ? detailFn(nextClaim) : `unknown verdict: ${nextClaim.verdict}`
}

export function nextClaimTone(nextClaim: NextClaimView): string {
  if (nextClaim.verdict === 'will-claim') return 'text-running-fg'
  return nextClaim.verdict === 'unknown' ? 'text-waiting-fg' : 'text-ink'
}
