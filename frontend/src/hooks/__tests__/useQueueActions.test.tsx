import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { useQueueActions } from '../useQueueActions'
import { queryKeys } from '../queryKeys'

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    {children}
  </QueryClientProvider>
)

beforeEach(() => server.use(...defaultHandlers))

it('surfaces ApiError.detail on action failure', async () => {
  server.use(
    http.post('/api/queue/boost', () =>
      HttpResponse.json({ detail: 'issue is blocked' }, { status: 422 }),
    ),
  )
  const { result } = renderHook(() => useQueueActions(), { wrapper })
  expect(result.current.queueError).toBeNull()
  act(() => { result.current.boost(51, 1) })
  await waitFor(() => expect(result.current.queueError).toBe('issue is blocked'))
})

it('success after failure clears the error — proven by requiring the error exists first', async () => {
  // Phase 1: trigger a failure so queueError is set.
  server.use(
    http.post('/api/queue/boost', () =>
      HttpResponse.json({ detail: 'boost failed' }, { status: 422 }),
    ),
    http.post('/api/queue/next', () =>
      HttpResponse.json({ ok: true, reason: 'queued' }),
    ),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const { result } = renderHook(() => useQueueActions(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    ),
  })

  // Produce the error first — if the error is never set the clear is untestable.
  act(() => { result.current.boost(51, 1) })
  await waitFor(() => expect(result.current.queueError).toBe('boost failed'))

  // Phase 2: a successful action must clear it.
  act(() => { result.current.next(51) })
  await waitFor(() => expect(result.current.queueError).toBeNull())

  // Phase 3: board AND task keys are invalidated.
  const calledKeys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))
  expect(calledKeys).toContain(JSON.stringify(queryKeys.board))
  expect(calledKeys).toContain(JSON.stringify(queryKeys.task(51)))
})

it('ready() posts to /api/queue/ready and invalidates board + task', async () => {
  server.use(
    http.post('/api/queue/ready', () =>
      HttpResponse.json({ ok: true, reason: 'marked ready' }),
    ),
  )
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const { result } = renderHook(() => useQueueActions(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    ),
  })
  act(() => { result.current.ready(73) })
  await waitFor(() =>
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.task(73) }),
  )
  const calledKeys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey))
  expect(calledKeys).toContain(JSON.stringify(queryKeys.board))
})

it('busy flag is true during an in-flight action and false afterward', async () => {
  let resolveBoost!: (v: Response) => void
  server.use(
    http.post('/api/queue/boost', () =>
      new Promise<Response>((resolve) => { resolveBoost = resolve }),
    ),
  )
  const { result } = renderHook(() => useQueueActions(), { wrapper })
  expect(result.current.busy).toBe(false)

  act(() => { result.current.boost(51, 1) })
  await waitFor(() => expect(result.current.busy).toBe(true))

  act(() => {
    resolveBoost(HttpResponse.json({ ok: true, reason: 'boosted' }) as unknown as Response)
  })
  await waitFor(() => expect(result.current.busy).toBe(false))
})
