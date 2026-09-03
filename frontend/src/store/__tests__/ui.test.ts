import { useUiStore } from '../ui'

beforeEach(() => {
  useUiStore.setState({ collapsedColumns: {} })
})

it('toggles a column collapsed and back', () => {
  useUiStore.getState().toggleColumn('parked')
  expect(useUiStore.getState().collapsedColumns.parked).toBe(true)
  useUiStore.getState().toggleColumn('parked')
  expect(useUiStore.getState().collapsedColumns.parked).toBe(false)
})

it('never persists column state (transient by design)', () => {
  useUiStore.getState().toggleColumn('parked')
  expect(localStorage.getItem('agent-ops-ui')).toBeNull()
})
