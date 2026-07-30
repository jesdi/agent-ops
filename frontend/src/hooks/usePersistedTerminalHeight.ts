import { useEffect, useRef } from 'react'
import { useUiStore } from '../store/ui'

/**
 * Reads the persisted terminal height from the store and returns a ref to
 * attach to the resizable wrapper element.  When the operator drags the
 * wrapper to a new height, a ResizeObserver writes the new value back to the
 * store (and therefore to localStorage), so the height survives page reloads.
 *
 * Guards:
 *   - Only writes when measured height > 0 (jsdom / layout-less environments
 *     report 0 for every element; zero is not a real drag target).
 *   - Only writes when measured height differs from the current store value
 *     (prevents a feedback loop: React re-renders `style={{ height }}` to the
 *     same value → observer could re-fire → measured === store → no write).
 *
 * The observer is created once (setTerminalHeight is a stable zustand
 * reference).  The latest store value is tracked via a mutable ref so the
 * callback always compares against the up-to-date value without causing a new
 * observer to be created on every height change.
 */
export function usePersistedTerminalHeight() {
  const terminalHeight = useUiStore((s) => s.terminalHeight)
  const setTerminalHeight = useUiStore((s) => s.setTerminalHeight)
  const ref = useRef<HTMLDivElement>(null)

  // Keep the latest store value accessible inside the observer callback
  // without listing it as an effect dependency (which would recreate the
  // observer on every drag tick).
  const heightRef = useRef(terminalHeight)
  useEffect(() => {
    heightRef.current = terminalHeight
  }, [terminalHeight])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const observer = new ResizeObserver(() => {
      const measured = el.offsetHeight
      if (measured > 0 && measured !== heightRef.current) {
        setTerminalHeight(measured)
      }
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [setTerminalHeight])

  return { ref, height: terminalHeight }
}
