import { create } from 'zustand'

interface UiState {
  selectedIssue: number | null
  terminalOpen: boolean
  collapsedColumns: Record<string, boolean>
  selectIssue: (issue: number | null) => void
  setTerminalOpen: (open: boolean) => void
  toggleColumn: (key: string) => void
}

export const useUiStore = create<UiState>((set) => ({
  selectedIssue: null,
  terminalOpen: false,
  collapsedColumns: {},
  selectIssue: (selectedIssue) => set({ selectedIssue }),
  setTerminalOpen: (terminalOpen) => set({ terminalOpen }),
  toggleColumn: (key) =>
    set((s) => ({
      collapsedColumns: { ...s.collapsedColumns, [key]: !s.collapsedColumns[key] },
    })),
}))
