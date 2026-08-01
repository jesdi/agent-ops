import { useEffect, useState } from 'react'
import type { NextClaimView } from '../lib/api'
import { formatDuration } from '../lib/format'

/** Maps each known verdict to a detail string factory.
 *  A lookup keyed by verdict guarantees an unhandled verdict cannot silently
 *  fall through to wrong copy — unknown keys hit the explicit fallback branch. */
const VERDICT_DETAIL: Partial<Record<string, (nc: NextClaimView) => string>> = {
  'will-claim':    (nc) => `will claim #${nc.next_issue}`,
  'budget-blocked':(nc) => `budget resets in ${formatDuration(nc.minutes_to_reset * 60)}`,
  'capacity-full': ()   => 'capacity full — waits for a free slot',
  'no-candidates': ()   => 'queue empty — nothing to claim',
  // claims-paused: the dispatcher pass will still run; only claiming is skipped
  // while a triage sweep is pending. Distinct from errors and from empty queue.
  'claims-paused': ()   => 'claiming paused — triage sweep pending',
}

/** Countdown is client-side (1s tick, zero requests) and re-anchors whenever
 *  the board payload changes — the SSE fingerprint includes pass.json. */
export function NextClaimLine({ nextClaim }: { nextClaim: NextClaimView }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  if (nextClaim.verdict === 'unknown') {
    return (
      <span data-testid="next-claim" className="rounded border border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-800">
        dispatcher not running? — no recent pass heartbeat
      </span>
    )
  }

  // Math.ceil so the display shows "6m" until the exact tick it flips, not
  // "5m 59s" when the render happens 1 ms after the ETA was calculated.
  const left = Math.ceil((new Date(nextClaim.next_pass_eta).getTime() - now) / 1000)
  const pass = left > 0 ? `next pass in ${formatDuration(left)}` : 'next pass due now'

  const detailFn = VERDICT_DETAIL[nextClaim.verdict]
  const detail = detailFn
    ? detailFn(nextClaim)
    : `unknown verdict: ${nextClaim.verdict}`

  const isGood = nextClaim.verdict === 'will-claim'
  return (
    <span data-testid="next-claim" className="text-sm text-gray-600">
      {pass} — <span className={isGood ? 'text-emerald-700' : 'text-gray-700'}>{detail}</span>
    </span>
  )
}
