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
          repository: 'widget', task_issue: 38, blocker_repo: 'jesdi/widget',
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
          repository: 'widget', task_issue: 38, blocker_repo: 'jesdi/widget',
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
    http.post('/api/task/widget/38/retry', () => {
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
    http.post('/api/task/widget/38/retry', () =>
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

// #43: two quarantined tasks sharing an issue number on different repositories,
// so the per-row state is proven to be keyed `${repository}#${task_issue}`.
const twoQuarantined = {
  quarantined: [
    {
      repository: 'widget', task_issue: 38, blocker_repo: 'jesdi/widget',
      blocker_issue: 39, fingerprint: 'f1', created_at: '2026-07-24T22:10:00Z',
      blocker_open: true,
    },
    {
      repository: 'gadget', task_issue: 38, blocker_repo: 'jesdi/gadget',
      blocker_issue: 40, fingerprint: 'f2', created_at: '2026-07-24T22:10:00Z',
      blocker_open: true,
    },
  ],
  fingerprints: [],
}

it('each Retry button names the task it retries', async () => {
  server.use(http.get('/api/failures', () => HttpResponse.json(twoQuarantined)))
  renderWithProviders(<FailuresPage />)
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Retry widget task #38' })).toBeInTheDocument(),
  )
  expect(screen.getByRole('button', { name: 'Retry gadget task #38' })).toBeInTheDocument()
})

it('an in-flight retry disables only its own row', async () => {
  let release!: () => void
  const held = new Promise<void>((resolve) => { release = resolve })
  server.use(
    http.get('/api/failures', () => HttpResponse.json(twoQuarantined)),
    http.post('/api/task/widget/38/retry', async () => {
      await held
      return HttpResponse.json({ status: 'pending', intent: '175-38-retry' }, { status: 202 })
    }),
  )
  renderWithProviders(<FailuresPage />)
  const buttons = await screen.findAllByRole('button', { name: /^Retry/ })
  expect(buttons).toHaveLength(2)
  const [widget, gadget] = buttons as [HTMLElement, HTMLElement]
  await userEvent.click(widget)
  await waitFor(() => expect(widget).toBeDisabled())
  expect(gadget).toBeEnabled()
  release()
  await waitFor(() => expect(widget).toBeEnabled())
})
