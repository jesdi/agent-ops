import type { ReactNode } from 'react'
import type { Column } from '../lib/api'
import { cardDropTarget, type DraggedCard } from './cardDrag'

export interface EmptyColumn {
  column: Column
  /** Degraded-state markers that must stay visible while the column is empty. */
  markers?: ReactNode
  /** When set, the chip accepts card drags, like the column it stands for. */
  onCardDrop?: (card: DraggedCard) => void
}

/** Empty columns as greyed zero-count chips, so "no failures" is a visible
 *  zero rather than a missing column. Derived from the board, never stored. */
export function CountStrip({ columns }: { columns: EmptyColumn[] }) {
  if (columns.length === 0) return null
  return (
    <ul aria-label="Empty columns" className="flex flex-wrap gap-2">
      {columns.map(({ column, markers, onCardDrop }) => (
        <li
          key={column.key}
          data-testid={`chip-${column.key}`}
          aria-label={`${column.title}: 0`}
          className="flex items-center gap-1.5 rounded-full border bg-surface-raised px-2.5 py-0.5 text-xs text-ink-muted"
          {...cardDropTarget(onCardDrop)}
        >
          <span>{column.title}</span>
          {markers}
          <span className="font-semibold">0</span>
        </li>
      ))}
    </ul>
  )
}
