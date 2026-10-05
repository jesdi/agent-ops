import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { http, HttpResponse } from 'msw'
import type { UsageView } from '../../lib/api'
import { usageRouted } from '../../test/fixtures'
import { server } from '../../test/msw-server'
import { useSetPriority } from '../useSetPriority'
import { queryKeys } from '../queryKeys'

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(queryKeys.usage, usageRouted)
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  const { result } = renderHook(() => useSetPriority(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    ),
  })
  return { qc, invalidate, result }
}

it('a stored mode lands in the cached usage and invalidates usage, board and tasks', async () => {
  server.use(http.post('/api/priority', () => HttpResponse.json({ ok: true, mode: 'openai' })))
  const { qc, invalidate, result } = setup()
  act(() => { result.current.setPriority('openai') })
  await waitFor(() => expect(qc.getQueryData<UsageView>(queryKeys.usage)?.priority.mode).toBe('openai'))
  expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey))
    .toEqual([queryKeys.usage, queryKeys.board, queryKeys.allTasks])
})

it('a refused mode leaves the cache alone and invalidates nothing', async () => {
  server.use(http.post('/api/priority', () => HttpResponse.json({ detail: 'not routed' }, { status: 422 })))
  const { qc, invalidate, result } = setup()
  act(() => { result.current.setPriority('openai') })
  await waitFor(() => expect(result.current.error).toBe('not routed'))
  expect(qc.getQueryData<UsageView>(queryKeys.usage)?.priority.mode).toBe('auto')
  expect(invalidate).not.toHaveBeenCalled()
})
