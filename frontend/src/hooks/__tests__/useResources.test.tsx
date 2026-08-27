import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { useTasks, useTaskDetail, useTaskHistory } from '../useResources'

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    {children}
  </QueryClientProvider>
)

beforeEach(() => server.use(...defaultHandlers))

it('useTasks resolves the board view', async () => {
  const { result } = renderHook(() => useTasks(), { wrapper })
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data?.capacity.max_slots).toBe(3)
  expect(result.current.data?.columns).toHaveLength(11)
})

it('useTaskDetail(42) resolves the parked task', async () => {
  const { result } = renderHook(() => useTaskDetail(42), { wrapper })
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data?.card.issue).toBe(42)
  expect(result.current.data?.session_alive).toBe(true)
})

it('useTaskHistory fetches only when enabled', async () => {
  server.use(
    http.get('/api/task/42/history', () =>
      HttpResponse.json({ text: 'scrollback body' }),
    ),
  )
  const { result, rerender } = renderHook(
    ({ on }: { on: boolean }) => useTaskHistory(42, on),
    { wrapper, initialProps: { on: false } },
  )
  expect(result.current.fetchStatus).toBe('idle') // disabled: no fetch
  rerender({ on: true })
  await waitFor(() =>
    expect(result.current.data).toEqual({ text: 'scrollback body' }),
  )
})
