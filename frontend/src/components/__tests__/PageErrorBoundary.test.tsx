import { lazy, Suspense } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppShell } from '../AppShell'
import { PageErrorBoundary } from '../PageErrorBoundary'
import { MemoryRouter } from 'react-router'

const BrokenPage = lazy(() => Promise.reject(new Error('Failed to fetch dynamically imported module')))

it('keeps the shell and offers a reload when a page import rejects', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const reload = vi.fn()
  Object.defineProperty(window, 'location', { value: { ...window.location, reload }, writable: true })

  render(
    <MemoryRouter>
      <AppShell>
        <PageErrorBoundary>
          <Suspense fallback={<p>loading page…</p>}>
            <BrokenPage />
          </Suspense>
        </PageErrorBoundary>
      </AppShell>
    </MemoryRouter>,
  )

  expect(await screen.findByRole('alert')).toHaveTextContent('this page could not be loaded')
  expect(screen.getByRole('link', { name: 'Board' })).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'reload' }))
  expect(reload).toHaveBeenCalledOnce()
})
