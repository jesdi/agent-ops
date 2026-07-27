import type { TaskCard } from '../lib/api'
import { TaskCardView } from './TaskCard'

export interface ColumnProps {
  column: { key: string; title: string; cards: TaskCard[] }
  pendingByIssue: ReadonlyMap<number, readonly string[]>
  collapsed: boolean
  onToggle: () => void
}

export function BoardColumn({ column, pendingByIssue, collapsed, onToggle }: ColumnProps) {
  return (
    <section data-testid={`column-${column.key}`} className="w-64 shrink-0">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between rounded bg-gray-100 px-2 py-1 text-left text-sm font-semibold"
      >
        <span>{column.title}</span>
        <span className="text-gray-500">{column.cards.length}</span>
      </button>
      {!collapsed && (
        <div className="mt-2 flex flex-col gap-2">
          {column.cards.map((card) => (
            <TaskCardView
              key={card.issue}
              card={card}
              pendingActions={pendingByIssue.get(card.issue)}
            />
          ))}
        </div>
      )}
    </section>
  )
}
