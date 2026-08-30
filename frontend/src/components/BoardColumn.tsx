import type { ReactNode } from 'react'
import type { TaskCard } from '../lib/api'
import { TaskCardView } from './TaskCard'

export interface DraggedCard {
  issue: number
  target: string
  title: string
}

export interface ColumnProps {
  column: { key: string; title: string; cards: TaskCard[] }
  /**
   * Keyed `${target}#${issue}`. A legacy (target-less) pending intent is
   * stored under the key `#${issue}` (empty target) and applies to any card
   * with that issue number, since the intent file predates the target field
   * and cannot be attributed to one target over another.
   */
  pendingByKey: ReadonlyMap<string, readonly string[]>
  collapsed: boolean
  onToggle: () => void
  /** Extra content (e.g. ghost cards) rendered after the task cards, hidden when collapsed. */
  extra?: ReactNode
  /** Added to the card count shown in the column header. */
  extraCount?: number
  /**
   * Alert content rendered inside the header button itself, visible even when
   * the column is collapsed. Use for degraded-state indicators that must never
   * be hidden (e.g. stale-queue marker, action error).
   */
  headerExtra?: ReactNode
  /** When set, the column accepts card drags and reports each drop. */
  onCardDrop?: (card: DraggedCard) => void
}

export function BoardColumn({ column, pendingByKey, collapsed, onToggle, extra, extraCount, headerExtra, onCardDrop }: ColumnProps) {
  return (
    <section
      data-testid={`column-${column.key}`}
      className="w-64 shrink-0"
      onDragOver={onCardDrop && ((e) => e.preventDefault())}
      onDrop={onCardDrop && ((e) => {
        e.preventDefault()
        const raw = e.dataTransfer.getData('application/x-agent-ops-card')
        if (!raw) return
        try {
          onCardDrop(JSON.parse(raw) as DraggedCard)
        } catch { /* foreign drag — ignore */ }
      })}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between rounded bg-gray-100 px-2 py-1 text-left text-sm font-semibold"
      >
        <span>{column.title}</span>
        <span className="flex items-center gap-1">
          {headerExtra}
          <span className="text-gray-500">{column.cards.length + (extraCount ?? 0)}</span>
        </span>
      </button>
      {!collapsed && (
        <div className="mt-2 flex flex-col gap-2">
          {column.cards.map((card) => (
            <TaskCardView
              // Issue numbers are per-target: alpha#73 and beta#73 must not
              // collide on one React key.
              key={`${card.target}#${card.issue}`}
              card={card}
              pendingActions={[
                ...(pendingByKey.get(`${card.target}#${card.issue}`) ?? []),
                ...(pendingByKey.get(`#${card.issue}`) ?? []),
              ]}
            />
          ))}
          {extra}
        </div>
      )}
    </section>
  )
}
