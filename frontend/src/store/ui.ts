import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface UiState {
  /**
   * Issue whose terminal is attached, or null. NEVER persisted (see
   * partialize below): attaching writes the `attached-<N>` marker and the
   * dispatcher declines to drive a task while it exists, so re-establishing
   * it on page load would silently stall that task.
   */
  terminalOpenFor: number | null
  /** Operator-dragged terminal/history height in px. Persisted. */
  terminalHeight: number
  collapsedColumns: Record<string, boolean>
  setTerminalOpenFor: (issue: number | null) => void
  setTerminalHeight: (px: number) => void
  toggleColumn: (key: string) => void
}

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      terminalOpenFor: null,
      terminalHeight: 384, // was h-96
      collapsedColumns: {},
      setTerminalOpenFor: (terminalOpenFor) => set({ terminalOpenFor }),
      setTerminalHeight: (terminalHeight) => set({ terminalHeight }),
      toggleColumn: (key) =>
        set((s) => ({
          collapsedColumns: {
            ...s.collapsedColumns,
            [key]: !s.collapsedColumns[key],
          },
        })),
    }),
    {
      name: 'agent-ops-ui',
      // Only the height survives reloads. terminalOpenFor and transient
      // column state must not.
      partialize: (s) => ({ terminalHeight: s.terminalHeight }),
    },
  ),
)
