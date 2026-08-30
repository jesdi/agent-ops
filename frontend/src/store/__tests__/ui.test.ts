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
  useUiStore.getState().setTerminalOpenFor('widget#7')
  useUiStore.getState().setTerminalHeight(400)
  const stored = JSON.parse(localStorage.getItem('agent-ops-ui') ?? '{}')
  expect(stored.state.terminalHeight).toBe(400)
  expect(stored.state).not.toHaveProperty('terminalOpenFor')
})

it('keys terminalOpenFor by target and issue, not issue alone', () => {
  // Two targets can hold the same issue number; the key must distinguish
  // them or attaching one auto-attaches the other on navigation.
  useUiStore.getState().setTerminalOpenFor('widget#7')
  expect(useUiStore.getState().terminalOpenFor).toBe('widget#7')
  expect(useUiStore.getState().terminalOpenFor).not.toBe('other#7')
})
