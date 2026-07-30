import { useEffect, useRef } from 'react'
import { useTaskHistory } from '../hooks/useResources'

/**
 * Explicit, natively-scrollable pane history. The live terminal has no
 * scrollback (tmux holds the alternate screen); this is where "scroll up"
 * goes. Opens scrolled to the bottom so the transition from the live screen
 * reads as continuous upward scrolling. Native scroll = trackpad precision
 * and momentum for free.
 */
export function TerminalHistory({
  issue,
  onClose,
}: {
  issue: number
  onClose: () => void
}) {
  const paneRef = useRef<HTMLPreElement>(null)
  const query = useTaskHistory(issue, true)

  useEffect(() => {
    const el = paneRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [query.data])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div data-testid="terminal-history" className="relative h-full">
      <button
        type="button"
        aria-label="Return to live terminal"
        data-testid="terminal-history-back"
        className="absolute right-2 top-2 z-10 rounded border bg-white px-2 py-0.5 text-xs shadow"
        onClick={onClose}
      >
        Back to live
      </button>
      <pre
        ref={paneRef}
        data-testid="terminal-history-pane"
        className="h-full w-full overflow-auto rounded bg-gray-900 p-3 font-mono text-xs text-gray-100"
      >
        {query.isPending ? 'loading history…' : (query.data?.text ?? '')}
      </pre>
    </div>
  )
}
