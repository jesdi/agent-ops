import type { DragEvent, ReactNode } from 'react'
import type { Column } from '../lib/api'
import { TaskCardView } from './TaskCard'

export interface DraggedCard {
  issue: number
  target: string
  title: string
}

/** Drop-target handlers for the card drag payload. Shared by the Wont do
 *  column and its count-strip chip, so both accept the same drags. */
export function cardDropTarget(onCardDrop: ((card: DraggedCard) => void) | undefined) {
  if (!onCardDrop) return {}
  return {
    onDragOver: (e: DragEvent) => e.preventDefault(),
    onDrop: (e: DragEvent) => {
      e.preventDefault()
      const raw = e.dataTransfer.getData('application/x-agent-ops-card')
      if (!raw) return
      try {
        onCardDrop(JSON.parse(raw) as DraggedCard)
      } catch { /* foreign drag — ignore */ }
    },
  }
}

export interface ColumnProps {
  column: Column
  /**
   * Keyed `${target}#${issue}`. A legacy (target-less) pending intent is
   * stored under the key `#${issue}` (empty target) and applies to any card
   * with that issue number, since the intent file predates the target field
   * and cannot be attributed to one target over another.
   */
  pendingByKey: ReadonlyMap<string, readonly string[]>
  /** Extra content (e.g. ghost cards) rendered after the task cards. */
  extra?: ReactNode
  /** Added to the card count shown in the column header. */
  extraCount?: number
  /** Degraded-state indicators shown beside the count (e.g. stale-queue marker, action error). */
  headerExtra?: ReactNode
  /** When set, the column accepts card drags and reports each drop. */
  onCardDrop?: (card: DraggedCard) => void
  /** Layout classes from the page, e.g. hiding an inactive tab on phones. */
  className?: string
}

export function BoardColumn({ column, pendingByKey, extra, extraCount, headerExtra, onCardDrop, className = '' }: ColumnProps) {
  return (
    <section
      id={`column-${column.key}`}
      data-testid={`column-${column.key}`}
      // Full width on phones, where it is the only column on screen.
      className={`flex min-h-0 w-full shrink-0 flex-col md:w-64 ${className}`}
      {...cardDropTarget(onCardDrop)}
    >
      {/* On phones the active tab already names the column and its count, so
          the header only shows when it carries degraded-state markers. */}
      <div className={`flex shrink-0 items-center justify-between rounded bg-ink/5 px-2 py-1 text-sm font-semibold ${
        headerExtra ? '' : 'max-md:hidden'}`}>
        <span>{column.title}</span>
        <span className="flex items-center gap-1">
          {headerExtra}
          <span className="text-ink-muted">{column.cards.length + (extraCount ?? 0)}</span>
        </span>
      </div>
      {/* The body scrolls on its own once the board caps its height; the
          header sits outside it, so it stays pinned. */}
      <div data-testid={`scroll-${column.key}`}
        className={`flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto ${headerExtra ? 'mt-2' : 'md:mt-2'}`}>
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
    </section>
  )
}
