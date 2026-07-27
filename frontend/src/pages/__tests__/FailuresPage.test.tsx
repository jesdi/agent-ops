import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { renderWithProviders } from '../../test/render'
import { FailuresPage } from '../FailuresPage'

beforeEach(() => server.use(...defaultHandlers))

it('renders quarantine entries joined to their blocker state', async () => {
  renderWithProviders(<FailuresPage />)
  await waitFor(() =>
    expect(screen.getByText('pytest::test_auth_flow')).toBeInTheDocument(),
  )
  expect(screen.getByText(/task #38/)).toBeInTheDocument()
  expect(screen.getByText(/blocker #39/)).toBeInTheDocument()
  expect(screen.getByText('blocker open')).toBeInTheDocument()
})

it('blocker_open null renders "blocker state unknown"', async () => {
  server.use(
    http.get('/api/failures', () =>
      HttpResponse.json({
        quarantined: [{
          target: 'jesdi/widget', task_issue: 38, blocker_repo: 'jesdi/widget',
          blocker_issue: 39, fingerprint: 'f', created_at: '2026-07-24T22:10:00Z',
          blocker_open: null,
        }],
        fingerprints: [],
      }),
    ),
  )
  renderWithProviders(<FailuresPage />)
  await waitFor(() =>
    expect(screen.getByText('blocker state unknown')).toBeInTheDocument(),
  )
})

it('blocker_open false renders "blocker closed" and not "blocker state unknown"', async () => {
  server.use(
    http.get('/api/failures', () =>
      HttpResponse.json({
        quarantined: [{
          target: 'jesdi/widget', task_issue: 38, blocker_repo: 'jesdi/widget',
          blocker_issue: 39, fingerprint: 'f', created_at: '2026-07-24T22:10:00Z',
          blocker_open: false,
        }],
        fingerprints: [],
      }),
    ),
  )
  renderWithProviders(<FailuresPage />)
  await waitFor(() =>
    expect(screen.getByText('blocker closed')).toBeInTheDocument(),
  )
  expect(screen.queryByText('blocker state unknown')).not.toBeInTheDocument()
})

it('retry posts the intent for the quarantined task', async () => {
  let retried = false
  server.use(
    http.post('/api/task/38/retry', () => {
      retried = true
      return HttpResponse.json(
        { status: 'pending', intent: '175-38-retry' }, { status: 202 },
      )
    }),
  )
  renderWithProviders(<FailuresPage />)
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument(),
  )
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(retried).toBe(true))
})

it('a failing retry surfaces the API detail inline instead of looking like success', async () => {
  server.use(
    http.post('/api/task/38/retry', () =>
      HttpResponse.json(
        { detail: 'issue 38 is not quarantined' }, { status: 404 },
      ),
    ),
  )
  renderWithProviders(<FailuresPage />)
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument(),
  )
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() =>
    expect(screen.getByTestId('retry-error-38')).toHaveTextContent(
      'issue 38 is not quarantined',
    ),
  )
})
