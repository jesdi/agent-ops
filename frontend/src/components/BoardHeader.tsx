import { useId, useState } from 'react'
import type { BoardSnapshot } from '../lib/api'
import { formatDuration } from '../lib/format'
import { nextClaimDetail, nextClaimTone } from '../lib/nextClaim'
import { useTasks, useUsage } from '../hooks/useResources'
import { CapacityMeter } from './CapacityMeter'
import { Chevron } from './Chevron'
import { NextClaimLine } from './NextClaimLine'
import { UsagePanel } from './UsagePanel'
import { banner } from '../lib/tone'

/** The row above the board: capacity, usage, the next-claim forecast and the
 *  median cycle time. Owns the usage query; the forecast rides on the full
 *  board query, which can arrive after the saved-cards snapshot. */
export function BoardHeader({ board }: { board: BoardSnapshot }) {
  const usageQuery = useUsage()
  const boardQuery = useTasks()
  const nextClaim = boardQuery.data?.next_claim
  // Phones only: the row collapses behind a one-line summary. Transient, so
  // the board always opens with the cards above the fold.
  const [open, setOpen] = useState(false)
  const rowId = useId()
  return (
    <div className="flex flex-col gap-3">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={rowId}
        onClick={() => setOpen((value) => !value)}
        className="-mx-1 flex min-w-0 items-center gap-1.5 rounded px-1 py-0.5 text-left text-sm md:hidden"
      >
        <span className="shrink-0 font-medium text-ink">
          {board.capacity.active}/{board.capacity.capacity} active
        </span>
        {/* Spaces between the flex items keep the accessible name readable. */}
        {' '}<span aria-hidden="true" className="text-ink-muted">·</span>{' '}
        {nextClaim ? (
          <span className={`min-w-0 truncate ${nextClaimTone(nextClaim)}`}>{nextClaimDetail(nextClaim)}</span>
        ) : (
          <span className="min-w-0 truncate text-ink-muted">
            {boardQuery.isError ? 'forecast unavailable' : 'loading forecast…'}
          </span>
        )}
        <span className="ml-auto text-ink-muted"><Chevron expanded={open} /></span>
      </button>
      <div id={rowId} className={`${open ? 'flex' : 'hidden'} flex-wrap items-center justify-between gap-4 md:flex`}>
        <CapacityMeter capacity={board.capacity} />
        {/* A failed /api/usage must not silently vanish the gauge — the
            operator would read "no gauge" as "nothing to worry about". */}
        {usageQuery.isError ? (
          <span data-testid="usage-error" className={banner.waiting}>
            usage unknown — {usageQuery.error.message}
          </span>
        ) : (
          usageQuery.data && <UsagePanel usage={usageQuery.data} />
        )}
        {nextClaim ? <NextClaimLine nextClaim={nextClaim} /> : (
          <span role="status" className="text-sm text-ink-muted">
            {boardQuery.isError ? 'queue and forecast unavailable' : 'loading queue and forecast…'}
          </span>
        )}
        {board.median_cycle_seconds != null && (
          <span className="text-sm text-ink-muted">
            ≈{formatDuration(board.median_cycle_seconds)} per task
          </span>
        )}
      </div>
    </div>
  )
}
