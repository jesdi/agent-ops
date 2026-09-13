import { StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Route, Routes } from 'react-router'
import './index.css'
import { AppShell } from './components/AppShell'
import { LiveUpdatesProvider } from './hooks/useLiveUpdates'
import { BoardPage } from './pages/BoardPage'
import { FailuresPage, HistoryPage, TaskPage } from './pages/LazyPages'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 1000, retry: 1 } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <LiveUpdatesProvider>
        <BrowserRouter>
          <AppShell>
            <Suspense fallback={<p className="p-4 text-gray-500">loading page…</p>}>
              <Routes>
                <Route path="/" element={<BoardPage />} />
                <Route path="/task/:target/:issue" element={<TaskPage />} />
                <Route path="/failures" element={<FailuresPage />} />
                <Route path="/history" element={<HistoryPage />} />
              </Routes>
            </Suspense>
          </AppShell>
        </BrowserRouter>
      </LiveUpdatesProvider>
    </QueryClientProvider>
  </StrictMode>,
)
