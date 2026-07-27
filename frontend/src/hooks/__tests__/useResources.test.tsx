import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { useTasks, useTaskDetail } from '../useResources'

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
  expect(result.current.data?.columns).toHaveLength(9)
})

it('useTaskDetail(42) resolves the parked task', async () => {
  const { result } = renderHook(() => useTaskDetail(42), { wrapper })
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data?.card.issue).toBe(42)
  expect(result.current.data?.session_alive).toBe(true)
})
