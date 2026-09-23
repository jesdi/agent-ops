import { useId, useState, type PointerEvent } from 'react'

/** Transient expand state for a board card. Never persisted: a reload
 *  always opens the board compact. */
export function useExpand() {
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

export function ExpandToggle({ expanded, controls, label, onToggle }: {
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
      <svg viewBox="0 0 16 16" aria-hidden="true"
        className={`h-3.5 w-3.5 transition-transform ${expanded ? 'rotate-90' : ''}`}>
        <path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </button>
  )
}
