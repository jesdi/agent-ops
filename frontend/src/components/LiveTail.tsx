import { useLayoutEffect, useRef, useState } from 'react'

/**
 * Live pane tail. Opens on the last line and follows new output while the
 * user is at the bottom; once they scroll up it stays put and offers a
 * jump-to-bottom control instead of yanking them back on every poll.
 */
export function LiveTail({ text }: { text: string }) {
  const paneRef = useRef<HTMLPreElement>(null)
  const [atBottom, setAtBottom] = useState(true)

  useLayoutEffect(() => {
    const el = paneRef.current
    if (el && atBottom) el.scrollTop = el.scrollHeight
  }, [text, atBottom])

  const handleScroll = () => {
    const el = paneRef.current
    if (el) setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight <= 4)
  }

  return (
    <div className="relative h-full">
      <pre
        ref={paneRef}
        data-testid="pane-tail"
        onScroll={handleScroll}
        className="h-full overflow-auto rounded bg-ink p-3 font-mono text-xs text-surface dark:bg-surface-raised dark:text-ink"
      >
        {text}
      </pre>
      {!atBottom && (
        <button
          type="button"
          aria-label="Scroll to bottom"
          className="absolute bottom-3 right-5 rounded border bg-surface-raised px-2 py-0.5 text-xs text-ink shadow dark:bg-surface"
          onClick={() => setAtBottom(true)}
        >
          ↓ Latest
        </button>
      )}
    </div>
  )
}
