import { DegradedMarkers, type ColumnView } from './BoardColumn'
import { cardDropTarget } from './cardDrag'

/** Empty columns as greyed zero-count chips, so "no failures" is a visible
 *  zero rather than a missing column. A chip keeps its column's degraded
 *  markers and drop target. Derived from the board, never stored. */
export function CountStrip({ columns }: { columns: ColumnView[] }) {
  if (columns.length === 0) return null
  return (
    <ul aria-label="Empty columns" className="flex flex-wrap gap-2">
      {columns.map(({ column, degraded, onCardDrop }) => (
        <li
          key={column.key}
          data-testid={`chip-${column.key}`}
          aria-label={`${column.title}: 0`}
          className="flex items-center gap-1.5 rounded-full border bg-surface-raised px-2.5 py-0.5 text-xs text-ink-muted"
          {...cardDropTarget(onCardDrop)}
        >
          <span>{column.title}</span>
          {degraded && <DegradedMarkers {...degraded} />}
          <span className="font-semibold">0</span>
        </li>
      ))}
    </ul>
  )
}
