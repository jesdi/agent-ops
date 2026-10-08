import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Outlet, Route, Routes } from 'react-router'
import './index.css'
import { AppShell } from './components/AppShell'
import { PageErrorBoundary } from './components/PageErrorBoundary'
import { LiveUpdatesProvider } from './hooks/useLiveUpdates'
import { BoardPage } from './pages/BoardPage'
import { FailuresPage, HistoryPage, ReviewPage, TaskPage } from './pages/LazyPages'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 1000, retry: 1 } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <LiveUpdatesProvider>
        <BrowserRouter>
          <Routes>
            {/* The review page fills the screen: no shell around it. */}
            <Route path="/task/:target/:issue/review" element={<PageErrorBoundary><ReviewPage /></PageErrorBoundary>} />
            <Route element={<AppShell><PageErrorBoundary><Outlet /></PageErrorBoundary></AppShell>}>
              <Route path="/" element={<BoardPage />} />
              <Route path="/task/:target/:issue" element={<TaskPage />} />
              <Route path="/failures" element={<FailuresPage />} />
              <Route path="/history" element={<HistoryPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </LiveUpdatesProvider>
    </QueryClientProvider>
  </StrictMode>,
)
