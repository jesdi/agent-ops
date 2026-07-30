import { useUiStore } from '../ui'

beforeEach(() => {
  localStorage.clear()
  useUiStore.setState({ terminalOpenFor: null, terminalHeight: 384 })
})

it('persists terminalHeight to localStorage', () => {
  useUiStore.getState().setTerminalHeight(560)
  expect(useUiStore.getState().terminalHeight).toBe(560)
  const stored = JSON.parse(localStorage.getItem('agent-ops-ui') ?? '{}')
  expect(stored.state.terminalHeight).toBe(560)
})

it('never persists terminalOpenFor (a persisted attach would stall a task)', () => {
  useUiStore.getState().setTerminalOpenFor(7)
  useUiStore.getState().setTerminalHeight(400)
  const stored = JSON.parse(localStorage.getItem('agent-ops-ui') ?? '{}')
  expect(stored.state.terminalHeight).toBe(400)
  expect(stored.state).not.toHaveProperty('terminalOpenFor')
})
