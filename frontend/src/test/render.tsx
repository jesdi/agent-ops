import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { LiveUpdatesContext } from '../hooks/useLiveUpdates'

export function renderWithProviders(
  ui: ReactElement,
  { route = '/', connected = true }: { route?: string; connected?: boolean } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <LiveUpdatesContext.Provider value={connected}>
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      </LiveUpdatesContext.Provider>
    </QueryClientProvider>
  )
  return { queryClient, ...render(ui, { wrapper }) }
}
