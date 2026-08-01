import type { ReactNode } from 'react'
import type { TaskCard } from '../lib/api'
import type { Accent } from '../lib/capacity'
import { TaskCardView } from './TaskCard'

export interface ColumnProps {
  column: { key: string; title: string; cards: TaskCard[] }
  pendingByIssue: ReadonlyMap<number, readonly string[]>
  collapsed: boolean
  onToggle: () => void
  accent: Accent
  /** Extra content (e.g. ghost cards) rendered after the task cards. */
  extra?: ReactNode
  /** Added to the card count shown in the column header. */
  extraCount?: number
}

export function BoardColumn({ column, pendingByIssue, collapsed, onToggle, accent, extra, extraCount }: ColumnProps) {
  return (
    <section data-testid={`column-${column.key}`} className="w-64 shrink-0">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between rounded bg-gray-100 px-2 py-1 text-left text-sm font-semibold"
      >
        <span>{column.title}</span>
        <span className="text-gray-500">{column.cards.length + (extraCount ?? 0)}</span>
      </button>
      {!collapsed && (
        <div className="mt-2 flex flex-col gap-2">
          {column.cards.map((card) => (
            <TaskCardView
              key={card.issue}
              card={card}
              pendingActions={pendingByIssue.get(card.issue)}
              accent={accent}
            />
          ))}
          {extra}
        </div>
      )}
    </section>
  )
}
