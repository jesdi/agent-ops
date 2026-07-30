import { useCallback, useRef } from 'react'
import { useUiStore } from '../store/ui'

/**
 * Reads the persisted terminal height from the store and returns a callback
 * ref to attach to the resizable wrapper element.  When the operator drags
 * the wrapper to a new height, a ResizeObserver writes the new value back to
 * the store (and therefore to localStorage), so the height survives page
 * reloads.
 *
 * Using a callback ref (useCallback) instead of useRef + useEffect means the
 * observer attaches/detaches whenever the wrapper node mounts or unmounts,
 * regardless of when that happens relative to the hook's first render.  This
 * correctly handles conditional rendering: the tail pane in TaskPage.tsx is
 * only rendered while the terminal is detached, so if the page loads while
 * the terminal is open the element is absent at first mount and a plain
 * useEffect would miss it.
 *
 * Guards:
 *   - Only writes when measured height > 0 (jsdom / layout-less environments
 *     report 0 for every element; zero is not a real drag target).
 *   - Only writes when measured height differs from the current store value
 *     (prevents a feedback loop: React re-renders `style={{ height }}` to the
 *     same value → observer could re-fire → measured === store → no write).
 *     The freshest store value is read via useUiStore.getState() at callback
 *     time to avoid stale closures without recreating the observer.
 */
export function usePersistedTerminalHeight() {
  const terminalHeight = useUiStore((s) => s.terminalHeight)
  const setTerminalHeight = useUiStore((s) => s.setTerminalHeight)

  // Holds the active observer so we can disconnect it when the node unmounts.
  const observerRef = useRef<ResizeObserver | null>(null)

  /**
   * Callback ref — React calls this with the node when it mounts and with
   * null when it unmounts.  Because the identity of this function is stable
   * (setTerminalHeight is a stable zustand reference), React only calls it at
   * mount/unmount, not on every render.
   */
  const ref = useCallback(
    (node: HTMLDivElement | null) => {
      // Disconnect any previous observer (node unmounted or changed)
      if (observerRef.current) {
        observerRef.current.disconnect()
        observerRef.current = null
      }
      if (!node) return
      // Guard: ResizeObserver may be undefined in some test environments
      // (jsdom + vitest global stub teardown can leave it unset when the
      // callback ref fires during React's synchronous commit phase).
      if (typeof ResizeObserver === 'undefined') return
      const observer = new ResizeObserver(() => {
        const measured = node.offsetHeight
        // Read the live store value at callback time — no stale closure risk.
        const current = useUiStore.getState().terminalHeight
        if (measured > 0 && measured !== current) {
          setTerminalHeight(measured)
        }
      })
      observer.observe(node)
      observerRef.current = observer
    },
    [setTerminalHeight],
  )

  return { ref, height: terminalHeight }
}
