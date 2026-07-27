import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Route, Routes } from 'react-router'
import './index.css'
import { AppShell } from './components/AppShell'
import { LiveUpdatesProvider } from './hooks/useLiveUpdates'
import { BoardPage } from './pages/BoardPage'
import { QueuePage } from './pages/QueuePage'
import { TaskPage } from './pages/TaskPage'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 1000, retry: 1 } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <LiveUpdatesProvider>
        <BrowserRouter>
          <AppShell>
            <Routes>
              <Route path="/" element={<BoardPage />} />
              <Route path="/queue" element={<QueuePage />} />
              <Route path="/task/:issue" element={<TaskPage />} />
            </Routes>
          </AppShell>
        </BrowserRouter>
      </LiveUpdatesProvider>
    </QueryClientProvider>
  </StrictMode>,
)
