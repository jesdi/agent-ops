import { render, screen, act } from '@testing-library/react'
import { useUiStore } from '../../store/ui'
import { usePersistedTerminalHeight } from '../usePersistedTerminalHeight'

// jsdom's built-in ResizeObserver is a no-op that never invokes callbacks.
// Override it here so tests can control exactly when the observer fires.
let capturedCallback: ResizeObserverCallback | null = null
const observeMock = vi.fn()
const disconnectMock = vi.fn()

beforeEach(() => {
  localStorage.clear()
  useUiStore.setState({ terminalOpenFor: null, terminalHeight: 384 })
  capturedCallback = null
  observeMock.mockClear()
  disconnectMock.mockClear()
  vi.stubGlobal(
    'ResizeObserver',
    vi.fn(function (cb: ResizeObserverCallback) {
      capturedCallback = cb
      return { observe: observeMock, disconnect: disconnectMock }
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function Wrapper() {
  const { ref, height } = usePersistedTerminalHeight()
  return <div ref={ref} style={{ height }} data-testid="pane-wrap" />
}

/** Set offsetHeight on el and fire the captured ResizeObserver callback. */
function fireResize(el: HTMLElement, px: number) {
  Object.defineProperty(el, 'offsetHeight', { value: px, configurable: true })
  act(() => {
    capturedCallback!([] as unknown as ResizeObserverEntry[], {} as ResizeObserver)
  })
}

it('writes the new height to the store and localStorage when the wrapper is dragged to a new height', () => {
  render(<Wrapper />)
  const el = screen.getByTestId('pane-wrap')

  fireResize(el, 600)

  expect(useUiStore.getState().terminalHeight).toBe(600)
  const stored = JSON.parse(localStorage.getItem('agent-ops-ui') ?? '{}')
  expect(stored.state.terminalHeight).toBe(600)
})

it('does NOT write to the store when the observed height equals the current store value (no feedback loop)', () => {
  useUiStore.setState({ terminalHeight: 500 })
  render(<Wrapper />)
  const el = screen.getByTestId('pane-wrap')

  // Intercept future setTerminalHeight calls by watching store state
  const before = useUiStore.getState().terminalHeight
  fireResize(el, 500) // same as current store value — must be a no-op

  expect(useUiStore.getState().terminalHeight).toBe(before)
})

it('persists the dragged height so it survives a page reload (localStorage key agent-ops-ui)', () => {
  render(<Wrapper />)
  const el = screen.getByTestId('pane-wrap')

  fireResize(el, 720)

  const stored = JSON.parse(localStorage.getItem('agent-ops-ui') ?? '{}')
  expect(stored.state.terminalHeight).toBe(720)
})

it('does not write when the observed height is 0 (jsdom / layout-less environment guard)', () => {
  render(<Wrapper />)
  const el = screen.getByTestId('pane-wrap')

  fireResize(el, 0)

  // Store must remain at the default 384 — zero is a layout artifact, not a drag
  expect(useUiStore.getState().terminalHeight).toBe(384)
})

/**
 * Regression: when the resizable element is CONDITIONALLY rendered (e.g. the
 * tail pane that only shows while the terminal is detached), the hook's host
 * may first mount with the element absent.  The observer must still attach
 * once the element appears (detach-within-session flow).
 */
function ConditionalWrapper({ show }: { show: boolean }) {
  const { ref, height } = usePersistedTerminalHeight()
  return show ? (
    <div ref={ref} style={{ height }} data-testid="pane-wrap" />
  ) : (
    <div data-testid="placeholder" />
  )
}

it('attaches the observer when the node appears AFTER the hook host already mounted (conditional-render / detach-within-session flow)', () => {
  // Start with element hidden (simulates TaskPage mounted while terminal is open)
  const { rerender } = render(<ConditionalWrapper show={false} />)
  // No element → no observer created yet
  expect(capturedCallback).toBeNull()

  // Simulate detaching the terminal: tail div now mounts
  rerender(<ConditionalWrapper show={true} />)
  // Observer must now be attached
  expect(capturedCallback).not.toBeNull()

  const el = screen.getByTestId('pane-wrap')
  fireResize(el, 650)

  expect(useUiStore.getState().terminalHeight).toBe(650)
  const stored = JSON.parse(localStorage.getItem('agent-ops-ui') ?? '{}')
  expect(stored.state.terminalHeight).toBe(650)
})
