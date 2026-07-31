import { BoardColumn } from '../components/BoardColumn'
import { BudgetBar } from '../components/BudgetBar'
import { CapacityMeter } from '../components/CapacityMeter'
import { capacityAccent } from '../lib/capacity'
import { useBudget, usePendingIntents, useTasks } from '../hooks/useResources'
import { useUiStore } from '../store/ui'

export function BoardPage() {
  const boardQuery = useTasks()
  const budgetQuery = useBudget()
  const intentsQuery = usePendingIntents()
  const collapsedColumns = useUiStore((s) => s.collapsedColumns)
  const toggleColumn = useUiStore((s) => s.toggleColumn)

  if (boardQuery.isPending) return <p className="p-4 text-gray-500">loading board…</p>
  if (boardQuery.isError) {
    return <p className="p-4 text-red-600">board unavailable: {boardQuery.error.message}</p>
  }

  const { columns, capacity } = boardQuery.data
  // An issue can carry several pending intents at once (park then kill).
  // Collapsing to one would silently drop the rest — and TaskPage renders
  // all of them, so the board must too.
  const pendingByIssue = new Map<number, string[]>()
  for (const i of intentsQuery.data?.intents ?? []) {
    pendingByIssue.set(i.issue, [...(pendingByIssue.get(i.issue) ?? []), i.action])
  }
  // Computed once so every column and every accented card agree on the colour.
  const accent = capacityAccent(capacity)

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <CapacityMeter capacity={capacity} />
        {/* A failed /api/budget must not silently vanish the gauge — the
            operator would read "no gauge" as "nothing to worry about". */}
        {budgetQuery.isError ? (
          <span
            data-testid="budget-error"
            className="rounded border border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-800"
          >
            usage unknown — budget unavailable: {budgetQuery.error.message}
          </span>
        ) : (
          budgetQuery.data && <BudgetBar budget={budgetQuery.data} />
        )}
      </div>
      <div className="flex gap-4 overflow-x-auto pb-4">
        {columns.map((column) => (
          <BoardColumn
            key={column.key}
            column={column}
            pendingByIssue={pendingByIssue}
            collapsed={collapsedColumns[column.key] ?? false}
            onToggle={() => toggleColumn(column.key)}
            accent={accent}
          />
        ))}
      </div>
    </div>
  )
}
