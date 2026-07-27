import { create } from 'zustand'

interface UiState {
  /**
   * Issue whose terminal is attached, or null. Issue-scoped rather than a
   * global boolean: attaching writes the `attached-<N>` marker and the
   * dispatcher declines to drive a task while that marker exists, so a global
   * flag would silently attach to — and therefore stall — every task the
   * operator browses to after their first attach.
   */
  terminalOpenFor: number | null
  collapsedColumns: Record<string, boolean>
  setTerminalOpenFor: (issue: number | null) => void
  toggleColumn: (key: string) => void
}

export const useUiStore = create<UiState>((set) => ({
  terminalOpenFor: null,
  collapsedColumns: {},
  setTerminalOpenFor: (terminalOpenFor) => set({ terminalOpenFor }),
  toggleColumn: (key) =>
    set((s) => ({
      collapsedColumns: { ...s.collapsedColumns, [key]: !s.collapsedColumns[key] },
    })),
}))
