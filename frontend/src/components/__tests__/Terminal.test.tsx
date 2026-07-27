import { act, render, screen } from '@testing-library/react'

const write = vi.fn()
const dispose = vi.fn()
vi.mock('@xterm/xterm', () => ({
  // vitest 4: vi.fn used as constructor must use function/class, not arrow
  Terminal: vi.fn(function () {
    return {
      cols: 80,
      rows: 24,
      loadAddon: vi.fn(),
      open: vi.fn(),
      write,
      onData: vi.fn(() => ({ dispose: vi.fn() })),
      onResize: vi.fn(() => ({ dispose: vi.fn() })),
      dispose,
    }
  }),
}))
vi.mock('@xterm/addon-fit', () => ({
  // vitest 4: vi.fn used as constructor must use function/class, not arrow
  FitAddon: vi.fn(function () { return { fit: vi.fn() } }),
}))
vi.mock('@xterm/xterm/css/xterm.css', () => ({}))

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static OPEN = 1
  binaryType = 'blob'
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  sent: unknown[] = []
  url: string
  constructor(url: string) { this.url = url; FakeWebSocket.instances.push(this) }
  send(data: unknown) { this.sent.push(data) }
  close() { this.onclose?.() }
}

beforeEach(() => {
  FakeWebSocket.instances = []
  write.mockClear()
  dispose.mockClear()
  vi.stubGlobal('WebSocket', FakeWebSocket)
  // vitest 4: vi.fn used as constructor must use function/class, not arrow
  vi.stubGlobal('ResizeObserver', vi.fn(function () {
    return { observe: vi.fn(), disconnect: vi.fn() }
  }))
})
afterEach(() => vi.unstubAllGlobals())

it('connects to the task terminal WS and sends an initial resize', async () => {
  const { Terminal } = await import('../Terminal')
  render(<Terminal issue={42} />)
  const ws = FakeWebSocket.instances[0]!
  expect(ws.url).toContain('/api/task/42/terminal')
  act(() => { ws.onopen?.() })
  expect(ws.sent[0]).toBe(JSON.stringify({ type: 'resize', cols: 80, rows: 24 }))
})

it('writes binary frames into xterm', async () => {
  const { Terminal } = await import('../Terminal')
  render(<Terminal issue={42} />)
  const ws = FakeWebSocket.instances[0]!
  const bytes = new TextEncoder().encode('hello').buffer
  act(() => { ws.onmessage?.({ data: bytes }) })
  expect(write).toHaveBeenCalledWith(new Uint8Array(bytes))
})

it('AWKWARD: dead message renders the fallback with the pane tail', async () => {
  const { Terminal } = await import('../Terminal')
  render(<Terminal issue={42} />)
  const ws = FakeWebSocket.instances[0]!
  act(() => {
    ws.onmessage?.({
      data: JSON.stringify({ type: 'dead', tail: 'last output line\n' }),
    })
  })
  expect(
    screen.getByText('session task-42 is not running'),
  ).toBeInTheDocument()
  expect(screen.getByTestId('terminal-dead').textContent).toContain(
    'last output line',
  )
})

it('dead → live navigation: re-render with new issue connects new WS and clears fallback', async () => {
  const { Terminal } = await import('../Terminal')
  const { rerender } = render(<Terminal issue={42} />)
  const ws42 = FakeWebSocket.instances[0]!
  // drive the dead frame on issue 42
  act(() => {
    ws42.onmessage?.({
      data: JSON.stringify({ type: 'dead', tail: 'done\n' }),
    })
  })
  expect(screen.getByText('session task-42 is not running')).toBeInTheDocument()
  // navigate to issue 43
  act(() => { rerender(<Terminal issue={43} />) })
  // a second WebSocket must have been created for task 43
  expect(FakeWebSocket.instances).toHaveLength(2)
  expect(FakeWebSocket.instances[1]!.url).toContain('/api/task/43/terminal')
  // dead fallback must be gone
  expect(screen.queryByTestId('terminal-dead')).not.toBeInTheDocument()
})
