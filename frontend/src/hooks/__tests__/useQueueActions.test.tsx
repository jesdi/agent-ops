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

it('invalidates board query on success and clears the error', async () => {
  server.use(
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
  act(() => { result.current.next(51) })
  await waitFor(() =>
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.board }),
  )
  expect(result.current.queueError).toBeNull()
})
