import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, act } from '@testing-library/react'
import { LiveUpdatesProvider, useLiveConnected } from '../useLiveUpdates'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  url: string
  constructor(url: string) { this.url = url; FakeEventSource.instances.push(this) }
  close() { this.closed = true }
}

function Probe() {
  const connected = useLiveConnected()
  return <span data-testid="probe">{connected ? 'live' : 'polling'}</span>
}

let qc: QueryClient

beforeEach(() => {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  vi.useFakeTimers()
  qc = new QueryClient()
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function setup() {
  return render(
    <QueryClientProvider client={qc}>
      <LiveUpdatesProvider><Probe /></LiveUpdatesProvider>
    </QueryClientProvider>,
  )
}

it('invalidates exactly the changed query keys on message', () => {
  const spy = vi.spyOn(qc, 'invalidateQueries')
  setup()
  const es = FakeEventSource.instances[0]!
  act(() => { es.onopen?.() })
  act(() => { es.onmessage?.({ data: '{"changed": ["board", "budget"]}' }) })
  const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))
  expect(keys).toContain(JSON.stringify(['board']))
  expect(keys).toContain(JSON.stringify(['budget']))
  expect(keys).toContain(JSON.stringify(['task']))          // board change touches task detail
  expect(keys).toContain(JSON.stringify(['pending-intents'])) // board confirms intents
  expect(keys).not.toContain(JSON.stringify(['queue']))
  expect(keys).not.toContain(JSON.stringify(['failures']))
})

it('a malformed frame is ignored, and the next good frame still invalidates', () => {
  const spy = vi.spyOn(qc, 'invalidateQueries')
  setup()
  const es = FakeEventSource.instances[0]!
  act(() => { es.onopen?.() })
  expect(() => {
    act(() => { es.onmessage?.({ data: 'not json at all' }) })
  }).not.toThrow()
  expect(() => {
    act(() => { es.onmessage?.({ data: '{"changed": "queue"}' }) })
  }).not.toThrow()
  expect(spy).not.toHaveBeenCalled()
  act(() => { es.onmessage?.({ data: '{"changed": ["queue"]}' }) })
  expect(spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))).toContain(
    JSON.stringify(['queue']),
  )
})

it('flips to polling on error and reconnects with backoff', () => {
  const { getByTestId } = setup()
  const first = FakeEventSource.instances[0]!
  act(() => { first.onopen?.() })
  expect(getByTestId('probe').textContent).toBe('live')

  act(() => { first.onerror?.() })
  expect(first.closed).toBe(true)
  expect(getByTestId('probe').textContent).toBe('polling')
  expect(FakeEventSource.instances).toHaveLength(1)

  act(() => { vi.advanceTimersByTime(1000) }) // first retry after 1s
  expect(FakeEventSource.instances).toHaveLength(2)

  const second = FakeEventSource.instances[1]!
  act(() => { second.onerror?.() })
  act(() => { vi.advanceTimersByTime(1999) }) // backoff doubled: not yet
  expect(FakeEventSource.instances).toHaveLength(2)
  act(() => { vi.advanceTimersByTime(1) })
  expect(FakeEventSource.instances).toHaveLength(3)

  act(() => { FakeEventSource.instances[2]!.onopen?.() })
  expect(getByTestId('probe').textContent).toBe('live')
})
