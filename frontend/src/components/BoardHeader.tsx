import type { BoardSnapshot } from '../lib/api'
import { formatDuration } from '../lib/format'
import { useTasks, useUsage } from '../hooks/useResources'
import { CapacityMeter } from './CapacityMeter'
import { NextClaimLine } from './NextClaimLine'
import { UsagePanel } from './UsagePanel'

/** The row above the board: capacity, usage, the next-claim forecast and the
 *  median cycle time. Owns the usage query; the forecast rides on the full
 *  board query, which can arrive after the saved-cards snapshot. */
export function BoardHeader({ board }: { board: BoardSnapshot }) {
  const usageQuery = useUsage()
  const boardQuery = useTasks()
  const nextClaim = boardQuery.data?.next_claim
  return (
    <div className="flex flex-wrap items-center justify-between gap-4">
      <CapacityMeter capacity={board.capacity} />
      {/* A failed /api/usage must not silently vanish the gauge — the
          operator would read "no gauge" as "nothing to worry about". */}
      {usageQuery.isError ? (
        <span data-testid="usage-error"
          className="rounded border border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          usage unknown — {usageQuery.error.message}
        </span>
      ) : (
        usageQuery.data && <UsagePanel usage={usageQuery.data} />
      )}
      {nextClaim ? <NextClaimLine nextClaim={nextClaim} /> : (
        <span role="status" className="text-sm text-gray-500">
          {boardQuery.isError ? 'queue and forecast unavailable' : 'loading queue and forecast…'}
        </span>
      )}
      {board.median_cycle_seconds != null && (
        <span className="text-sm text-gray-500">
          ≈{formatDuration(board.median_cycle_seconds)} per task
        </span>
      )}
    </div>
  )
}
