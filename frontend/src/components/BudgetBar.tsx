import type { BudgetView } from '../lib/api'
import { formatUtilization } from '../lib/format'

export function BudgetBar({ budget }: { budget: BudgetView }) {
  if (budget.source === 'unavailable') {
    return (
      <div className="rounded border border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-800">
        usage unknown — dispatcher will not spawn
      </div>
    )
  }
  const pct = formatUtilization(budget.utilization)
  const hours = Math.floor(budget.minutes_to_reset / 60)
  const mins = budget.minutes_to_reset % 60
  return (
    <div className="flex items-center gap-3 text-sm">
      <div
        role="progressbar"
        aria-valuenow={Math.round(budget.utilization * 100)}
        className="h-2 w-40 overflow-hidden rounded bg-gray-200"
      >
        <div
          className={budget.utilization > 0.85 ? 'h-full bg-red-500' : 'h-full bg-emerald-500'}
          style={{ width: pct }}
        />
      </div>
      <span className="font-medium">{pct}</span>
      <span className="text-gray-500">
        resets in {hours > 0 ? `${hours}h ` : ''}{mins}m
      </span>
      <span className={budget.would_spawn ? 'text-emerald-600' : 'text-red-600'}>
        {budget.would_spawn ? 'will spawn' : 'will not spawn'}
        {budget.threshold_applied === 'reset-racing' ? ' (reset-racing)' : ''}
      </span>
      <span className="text-xs text-gray-400">via {budget.source}</span>
    </div>
  )
}
