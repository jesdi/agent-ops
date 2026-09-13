import { lazy, Suspense } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { AppShell } from '../AppShell'
import { PageErrorBoundary } from '../PageErrorBoundary'

const BrokenTaskPage = lazy(() => Promise.reject(new Error('Failed to fetch dynamically imported module')))

function renderApp() {
  return render(
    <MemoryRouter initialEntries={['/task/repo/1']}>
      <AppShell>
        <PageErrorBoundary>
          <Suspense fallback={<p>loading page…</p>}>
            <Routes>
              <Route path="/" element={<p>board content</p>} />
              <Route path="/task/:target/:issue" element={<BrokenTaskPage />} />
            </Routes>
          </Suspense>
        </PageErrorBoundary>
      </AppShell>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

it('offers a reload when a page import rejects', async () => {
  const reload = vi.fn()
  Object.defineProperty(window, 'location', { value: { ...window.location, reload }, writable: true })
  renderApp()

  expect(await screen.findByRole('alert')).toHaveTextContent('this page could not be loaded')
  await userEvent.click(screen.getByRole('button', { name: 'reload' }))
  expect(reload).toHaveBeenCalledOnce()
})

it('recovers when the user navigates to another page via the shell', async () => {
  renderApp()
  await screen.findByRole('alert')

  await userEvent.click(screen.getByRole('link', { name: 'Board' }))

  expect(await screen.findByText('board content')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
