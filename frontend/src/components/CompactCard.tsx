import { useId, useState, type PointerEvent, type ReactNode } from 'react'
import { Link } from 'react-router'
import { Chevron } from './Chevron'
import { cardDragSource, type DraggedCard } from './cardDrag'

const VARIANT = {
  /** Work in flight: raised, solid, with a left accent for the slot. */
  task: { root: 'border-l-4 bg-surface-raised shadow-sm', title: 'text-ink' },
  /** A forecast, not work in flight: muted and dashed. */
  ghost: { root: 'border-dashed bg-surface text-ink-muted', title: '' },
} as const

/** The one compact board card: identifier row with expand toggle, clamped
 *  title link, and a detail block behind expand. The card is the drag source
 *  and, on touch, a tap target for expand; the title is the only link. */
export function CompactCard({ testId, drag, to, variant, accent = '', badges, stamp, signals, detail }: {
  testId: string
  drag: DraggedCard
  to: string
  variant: keyof typeof VARIANT
  /** Extra root classes, e.g. the slot's left-border hue. */
  accent?: string
  /** After the identifier; wraps among themselves. */
  badges?: ReactNode
  /** Top-right, before the expand toggle. */
  stamp?: ReactNode
  /** Under the title, always visible. */
  signals?: ReactNode
  /** Shown only when expanded. */
  detail: ReactNode
}) {
  const { expanded, detailId, toggle, onPointerUp } = useExpand()
  const id = `${drag.target}#${drag.issue}`
  const style = VARIANT[variant]
  return (
    <article
      data-testid={testId}
      {...cardDragSource(drag)}
      onPointerUp={onPointerUp}
      className={`rounded border px-2.5 py-2 ${style.root} ${accent}`}
    >
      {/* Badges wrap among themselves; the stamp and chevron stay top-right. */}
      <div className="flex items-start gap-2 text-xs text-ink-muted">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span>{id}</span>
          {badges}
        </div>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {stamp}
          <ExpandToggle expanded={expanded} controls={detailId} label={`Details for ${id}`}
            onToggle={toggle} />
        </span>
      </div>
      <Link
        to={to}
        draggable={false}
        // Not `block` alongside the clamp: `.block` sorts after `.line-clamp-2`
        // and would override its display, so the clamp would never apply.
        className={`mt-0.5 text-sm font-medium hover:underline ${style.title} ${
          expanded ? 'block' : 'line-clamp-2'
        }`}
      >
        {drag.title}
      </Link>
      {signals}
      {expanded && <div id={detailId} className="mt-2 border-t pt-2 text-xs">{detail}</div>}
    </article>
  )
}

/** Transient expand state. Never persisted: a reload always opens the board
 *  compact. */
function useExpand() {
  const [expanded, setExpanded] = useState(false)
  const detailId = useId()
  const toggle = () => setExpanded((value) => !value)
  // On touch the card body is a tap target for expand; links, buttons and
  // dialogs inside the card keep their own behaviour. A scroll gesture ends
  // in pointercancel, not pointerup, so panning never expands.
  const onPointerUp = (event: PointerEvent<HTMLElement>) => {
    if (event.pointerType !== 'touch') return
    if ((event.target as Element).closest('a, button, [role="dialog"]')) return
    toggle()
  }
  return { expanded, detailId, toggle, onPointerUp }
}

function ExpandToggle({ expanded, controls, label, onToggle }: {
  expanded: boolean
  /** id of the detail block this toggle shows and hides */
  controls: string
  label: string
  onToggle: () => void
}) {
  return (
    <button
      type="button"
      aria-expanded={expanded}
      aria-controls={controls}
      aria-label={label}
      onClick={onToggle}
      className="-m-1 shrink-0 rounded p-1 text-ink-muted hover:text-ink"
    >
      <Chevron expanded={expanded} />
    </button>
  )
}
