import type { ReactNode } from 'react'
import type { Column } from '../lib/api'
import { TaskCardView } from './TaskCard'
import { cardDropTarget, type DraggedCard } from './cardDrag'

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
  /** Shown in the column header: the page owns it, since Queued also counts ghosts. */
  count: number
  /** Degraded-state indicators shown beside the count (e.g. stale-queue marker, action error). */
  headerExtra?: ReactNode
  /** When set, the column accepts card drags and reports each drop. */
  onCardDrop?: (card: DraggedCard) => void
  /** Layout classes from the page, e.g. hiding an inactive tab on phones. */
  className?: string
}

export function BoardColumn({ column, count, pendingByKey, extra, headerExtra, onCardDrop, className = '' }: ColumnProps) {
  return (
    <section
      id={`column-${column.key}`}
      // The phone tab row controls these; from md up the tabs are hidden.
      role="tabpanel"
      aria-labelledby={`tab-${column.key}`}
      data-testid={`column-${column.key}`}
      // Full width on phones, where it is the only column on screen.
      className={`flex min-h-0 w-full shrink-0 flex-col md:w-64 ${className}`}
      {...cardDropTarget(onCardDrop)}
    >
      {/* On phones the active tab already names the column and its count, so
          the header only shows when it carries degraded-state markers. */}
      <div className={`flex shrink-0 flex-wrap items-center justify-between gap-x-2 gap-y-1 rounded bg-ink/5 px-2 py-1 text-sm font-semibold ${
        headerExtra ? '' : 'max-md:hidden'}`}>
        <span>{column.title}</span>
        <span className="text-ink-muted">{count}</span>
        {/* Markers get their own line: an error message is too long to share
            a 16rem row with the title and count. */}
        {headerExtra && <span className="flex basis-full flex-wrap items-center gap-1">{headerExtra}</span>}
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
