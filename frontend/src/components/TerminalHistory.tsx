import { useEffect, useRef } from 'react'
import { useTaskHistory } from '../hooks/useResources'

/**
 * Scrollable pane history. Shows a polled snapshot of the session's
 * scrollback (live view during the session, static snapshot after end).
 * The embedded terminal was removed with the tmux→herdr migration.
 * Attach is external via `herdr --remote box`.
 */
export function TerminalHistory({
  target,
  issue,
  onClose,
}: {
  target: string
  issue: number
  onClose: () => void
}) {
  const paneRef = useRef<HTMLPreElement>(null)
  // Auto-return to live when the user scrolls back down to the bottom —
  // native-scrollback behavior (iTerm/tmux). Armed only after the user has
  // actually been above the bottom: the pane OPENS scrolled to the bottom,
  // so an unarmed close would dismiss the overlay on mount.
  const armedRef = useRef(false)

  const handleScroll = () => {
    const el = paneRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= 4
    if (!atBottom) {
      armedRef.current = true
    } else if (armedRef.current) {
      armedRef.current = false
      onClose()
    }
  }

  const query = useTaskHistory(target, issue, true)

  useEffect(() => {
    const el = paneRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
      // Disarm auto-return after programmatic scroll: the pane is now at the
      // bottom, so armedRef must be false to reflect the true state. The next
      // manual scroll up will re-arm it.
      armedRef.current = false
    }
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
        onScroll={handleScroll}
        className="h-full w-full overflow-auto rounded bg-gray-900 p-3 font-mono text-xs text-gray-100"
      >
        {query.isPending ? 'loading history…' : (query.data?.text ?? '')}
      </pre>
    </div>
  )
}
