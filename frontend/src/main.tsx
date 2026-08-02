import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Route, Routes } from 'react-router'
import './index.css'
import { AppShell } from './components/AppShell'
import { LiveUpdatesProvider } from './hooks/useLiveUpdates'
import { BoardPage } from './pages/BoardPage'
import { FailuresPage } from './pages/FailuresPage'
import { HistoryPage } from './pages/HistoryPage'
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
              <Route path="/task/:issue" element={<TaskPage />} />
              <Route path="/failures" element={<FailuresPage />} />
              <Route path="/history" element={<HistoryPage />} />
            </Routes>
          </AppShell>
        </BrowserRouter>
      </LiveUpdatesProvider>
    </QueryClientProvider>
  </StrictMode>,
)
