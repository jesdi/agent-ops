import { BoardColumn } from '../components/BoardColumn'
import { BudgetBar } from '../components/BudgetBar'
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
  const pendingByIssue = new Map(
    (intentsQuery.data?.intents ?? []).map((i) => [i.issue, i.action]),
  )

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <span className="text-sm text-gray-600">
          {capacity.active}/{capacity.capacity} active · slots {capacity.slots_used}/{capacity.max_slots}
        </span>
        {budgetQuery.data && <BudgetBar budget={budgetQuery.data} />}
      </div>
      <div className="flex gap-4 overflow-x-auto pb-4">
        {columns.map((column) => (
          <BoardColumn
            key={column.key}
            column={column}
            pendingByIssue={pendingByIssue}
            collapsed={collapsedColumns[column.key] ?? false}
            onToggle={() => toggleColumn(column.key)}
          />
        ))}
      </div>
    </div>
  )
}
