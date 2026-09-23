import type { ReactNode } from 'react'
import type { Column } from '../lib/api'
import { chip } from '../lib/tone'
import { TaskCardView } from './TaskCard'
import { cardDropTarget, type DraggedCard } from './cardDrag'

/** Degraded state a column must never hide, whatever form it takes on screen. */
export interface Degraded {
  /** The ranking behind the ghosts failed; a cached one is shown. */
  stale: boolean
  /** The last queue action's error. */
  error: string | null
}

/** One column as the page shows it. The row, the count strip and the phone
 *  tabs are all filters over one list of these. */
export interface ColumnView {
  column: Column
  /** Cards plus ghosts: a column with a zero count is a chip. */
  count: number
  degraded: Degraded | null
  /** When set, the column (or its chip) accepts card drags and reports each drop. */
  onCardDrop?: (card: DraggedCard) => void
}

/** Visible text, not a tooltip: touch has no hover. */
export function DegradedMarkers({ stale, error }: Degraded) {
  return (
    <>
      {stale && (
        <span
          data-testid="queue-stale"
          className={chip.waiting}
          title="queue order may be outdated"
        >
          stale
        </span>
      )}
      {error && (
        <span
          data-testid="queue-error"
          className={chip.failed}
        >
          {error}
        </span>
      )}
    </>
  )
}

export function BoardColumn({ view: { column, count, degraded, onCardDrop }, pendingByKey, active, tabbed, children }: {
  view: ColumnView
  /**
   * Keyed `${target}#${issue}`. A legacy (target-less) pending intent is
   * stored under the key `#${issue}` (empty target) and applies to any card
   * with that issue number, since the intent file predates the target field
   * and cannot be attributed to one target over another.
   */
  pendingByKey: ReadonlyMap<string, readonly string[]>
  /** The phone tab on screen; below md every other column is hidden. */
  active: boolean
  /** The phone tab row is in use, so this column is its tab panel. */
  tabbed: boolean
  /** The column's ghosts, rendered after its task cards. */
  children?: ReactNode
}) {
  return (
    <section
      id={`column-${column.key}`}
      role={tabbed ? 'tabpanel' : undefined}
      aria-labelledby={tabbed ? `tab-${column.key}` : undefined}
      data-testid={`column-${column.key}`}
      // Full width on phones, where it is the only column on screen.
      className={`flex min-h-0 w-full shrink-0 flex-col gap-2 md:w-64 ${active ? '' : 'max-md:hidden'}`}
      {...cardDropTarget(onCardDrop)}
    >
      {/* On phones the active tab already names the column and its count, so
          the header only shows there to carry degraded state. */}
      <div className={`flex shrink-0 flex-wrap items-center justify-between gap-x-2 gap-y-1 rounded bg-ink/5 px-2 py-1 text-sm font-semibold ${
        degraded ? '' : 'max-md:hidden'}`}>
        <span>{column.title}</span>
        <span className="text-ink-muted">{count}</span>
        {/* Markers get their own line: an error message is too long to share
            a 16rem row with the title and count. */}
        {degraded && (
          <span className="flex basis-full flex-wrap items-center gap-1"><DegradedMarkers {...degraded} /></span>
        )}
      </div>
      {/* The body scrolls on its own once the board caps its height; the
          header sits outside it, so it stays pinned. */}
      <div data-testid={`scroll-${column.key}`} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
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
        {children}
      </div>
    </section>
  )
}
