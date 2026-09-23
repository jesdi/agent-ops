import { useEffect, useState } from 'react'
import type { NextClaimView } from '../lib/api'
import { formatDuration } from '../lib/format'
import { nextClaimDetail, nextClaimTone } from '../lib/nextClaim'
import { banner } from '../lib/tone'

/** Countdown is client-side (1s tick, zero requests) and re-anchors whenever
 *  the board payload changes — the SSE fingerprint includes pass.json. */
export function NextClaimLine({ nextClaim }: { nextClaim: NextClaimView }) {
  const [now, setNow] = useState(() => Date.now())
  // Hook is unconditional (rules of hooks); interval is gated on verdict so a
  // dead dispatcher ('unknown') does not tick at 1 Hz indefinitely while `now`
  // is never read.
  useEffect(() => {
    if (nextClaim.verdict === 'unknown') return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [nextClaim.verdict])

  if (nextClaim.verdict === 'unknown') {
    return (
      <span data-testid="next-claim" className={banner.waiting}>
        dispatcher not running? — no recent pass heartbeat
      </span>
    )
  }

  // Math.ceil so the display shows "6m" until the exact tick it flips, not
  // "5m 59s" when the render happens 1 ms after the ETA was calculated.
  // Guard against an unparseable ETA (empty string or bad ISO) — getTime()
  // returns NaN in that case, which would silently render "due now" (a lie).
  const etaMs = new Date(nextClaim.next_pass_eta).getTime()
  const left = isFinite(etaMs) ? Math.ceil((etaMs - now) / 1000) : null
  const pass =
    left === null ? 'next pass time unknown'
    : left > 0   ? `next pass in ${formatDuration(left)}`
    :               'next pass due now'

  return (
    <span data-testid="next-claim" className="text-sm text-ink-muted">
      {pass} — <span className={nextClaimTone(nextClaim)}>{nextClaimDetail(nextClaim)}</span>
    </span>
  )
}

