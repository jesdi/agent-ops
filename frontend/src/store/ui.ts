import { create } from 'zustand'

interface UiState {
  /** Board columns the operator collapsed. Transient: never persisted. */
  collapsedColumns: Record<string, boolean>
  toggleColumn: (key: string) => void
}

export const useUiStore = create<UiState>()((set) => ({
  collapsedColumns: {},
  toggleColumn: (key) =>
    set((s) => ({
      collapsedColumns: {
        ...s.collapsedColumns,
        [key]: !s.collapsedColumns[key],
      },
    })),
}))
