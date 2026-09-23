import { useEffect, useRef, type KeyboardEvent } from 'react'
import type { ColumnView } from './BoardColumn'

const STEP: Partial<Record<string, (index: number, last: number) => number>> = {
  ArrowRight: (i, last) => (i === last ? 0 : i + 1),
  ArrowLeft: (i, last) => (i === 0 ? last : i - 1),
  Home: () => 0,
  End: (_, last) => last,
}

/** The phone board's column picker: one tab per occupied column, in the order
 *  received, each naming its count. Hidden from md up, where every column
 *  is on screen. Selection follows focus; arrows, Home and End move it. */
export function ColumnTabs({ tabs, active, onSelect }: {
  tabs: ColumnView[]
  active: string | undefined
  onSelect: (key: string) => void
}) {
  const refs = useRef(new Map<string, HTMLButtonElement>())

  // A deep-linked tab can start off the right edge of the row.
  useEffect(() => {
    if (active) refs.current.get(active)?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }, [active])

  if (tabs.length === 0) return null

  const onKeyDown = (event: KeyboardEvent) => {
    const step = STEP[event.key]
    if (!step) return
    event.preventDefault()
    const index = tabs.findIndex((t) => t.column.key === active)
    const { key } = tabs[step(Math.max(index, 0), tabs.length - 1)]!.column
    onSelect(key)
    refs.current.get(key)?.focus()
  }

  return (
    <div
      role="tablist"
      aria-label="Columns"
      onKeyDown={onKeyDown}
      // Sticks to the top while a long column scrolls the page, so another
      // column is always one tap away.
      className="sticky top-0 z-10 -mx-4 flex overflow-x-auto border-b border-border bg-surface px-2 md:hidden"
    >
      {tabs.map(({ column: { key, title }, count }) => {
        const selected = key === active
        return (
          <button
            key={key}
            ref={(el) => { if (el) refs.current.set(key, el); else refs.current.delete(key) }}
            type="button"
            role="tab"
            id={`tab-${key}`}
            aria-selected={selected}
            aria-controls={`column-${key}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onSelect(key)}
            className={`-mb-px flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-2 py-2.5 text-sm ${
              selected ? 'border-ink font-semibold text-ink' : 'border-transparent text-ink-muted hover:text-ink'}`}
          >
            {title}{' '}
            <span className="rounded-full bg-ink/5 px-1.5 text-xs font-medium text-ink-muted">{count}</span>
          </button>
        )
      })}
    </div>
  )
}
