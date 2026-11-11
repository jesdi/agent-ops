import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { renderWithProviders } from '../../test/render'
import { FailuresPage } from '../FailuresPage'
import { FailureList } from '../../components/FailureList'

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
          blocker_open: null, readable: true,
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
          blocker_open: false, readable: true,
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
    expect(within(screen.getByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })).toBeInTheDocument(),
  )
  await userEvent.click(within(screen.getByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' }))
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
    expect(within(screen.getByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })).toBeInTheDocument(),
  )
  await userEvent.click(within(screen.getByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' }))
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
      blocker_open: true, readable: true,
    },
    {
      repository: 'gadget', task_issue: 38, blocker_repo: 'jesdi/gadget',
      blocker_issue: 40, fingerprint: 'f2', created_at: '2026-07-24T22:10:00Z',
      blocker_open: true, readable: true,
    },
  ],
  fingerprints: [],
}

it('each Retry row names its task and keeps the Retry button name', async () => {
  server.use(http.get('/api/failures', () => HttpResponse.json(twoQuarantined)))
  renderWithProviders(<FailuresPage />)
  await waitFor(() =>
    expect(within(screen.getByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })).toBeInTheDocument(),
  )
  expect(within(screen.getByRole('group', { name: 'gadget#38' })).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  expect(within(screen.getByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })).toHaveTextContent(/^Retry$/)
  expect(within(screen.getByRole('group', { name: 'gadget#38' })).getByRole('button', { name: 'Retry' })).toHaveTextContent(/^Retry$/)
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
  const widget = within(await screen.findByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })
  const gadget = within(screen.getByRole('group', { name: 'gadget#38' })).getByRole('button', { name: 'Retry' })
  await userEvent.click(widget)
  await waitFor(() => expect(widget).toBeDisabled())
  expect(gadget).toBeEnabled()
  release()
  await waitFor(() => expect(widget).toBeEnabled())
})

const ok = () =>
  HttpResponse.json({ status: 'pending', intent: '175-38-retry' }, { status: 202 })

function gate() {
  let release!: () => void
  const held = new Promise<void>((resolve) => { release = resolve })
  return { held, release }
}

async function twoButtons() {
  renderWithProviders(<FailuresPage />)
  const widget = within(await screen.findByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })
  const gadget = within(screen.getByRole('group', { name: 'gadget#38' })).getByRole('button', { name: 'Retry' })
  return [widget, gadget] as const
}

it('a gadget retry in flight disables gadget and leaves widget enabled', async () => {
  const g = gate()
  server.use(
    http.get('/api/failures', () => HttpResponse.json(twoQuarantined)),
    http.post('/api/task/gadget/38/retry', async () => { await g.held; return ok() }),
  )
  const [widget, gadget] = await twoButtons()
  await userEvent.click(gadget)
  await waitFor(() => expect(gadget).toBeDisabled())
  expect(widget).toBeEnabled()
  g.release()
  await waitFor(() => expect(gadget).toBeEnabled())
})

it('two retries in flight settle independently', async () => {
  const w = gate()
  const g = gate()
  server.use(
    http.get('/api/failures', () => HttpResponse.json(twoQuarantined)),
    http.post('/api/task/widget/38/retry', async () => { await w.held; return ok() }),
    http.post('/api/task/gadget/38/retry', async () => { await g.held; return ok() }),
  )
  const [widget, gadget] = await twoButtons()
  await userEvent.click(widget)
  await waitFor(() => expect(widget).toBeDisabled())
  await userEvent.click(gadget)
  await waitFor(() => expect(gadget).toBeDisabled())
  expect(widget).toBeDisabled()
  w.release()
  await waitFor(() => expect(widget).toBeEnabled())
  expect(gadget).toBeDisabled()
  g.release()
  await waitFor(() => expect(gadget).toBeEnabled())
})

it('a failed retry re-enables its button and shows its error only in its row', async () => {
  server.use(
    http.get('/api/failures', () => HttpResponse.json(twoQuarantined)),
    http.post('/api/task/widget/38/retry', () =>
      HttpResponse.json({ detail: 'issue 38 is not quarantined' }, { status: 404 }),
    ),
  )
  renderWithProviders(<FailuresPage />)
  const widget = within(await screen.findByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })
  await userEvent.click(widget)
  const gadget = within(screen.getByRole('group', { name: 'gadget#38' })).getByRole('button', { name: 'Retry' })
  const widgetRow = screen.getByRole('group', { name: 'widget#38' })
  const gadgetRow = screen.getByRole('group', { name: 'gadget#38' })
  expect(widgetRow).not.toBe(gadgetRow)
  await waitFor(() =>
    expect(within(widgetRow).getByTestId('retry-error-38')).toHaveTextContent(
      'issue 38 is not quarantined',
    ),
  )
  expect(within(gadgetRow).queryByTestId('retry-error-38')).not.toBeInTheDocument()
  expect(screen.getAllByTestId('retry-error-38')).toHaveLength(1)
  expect(widget).toBeEnabled()
  expect(gadget).toBeEnabled()
})

// #87: a quarantine record nobody can read still blocks its task, so it is
// listed explicitly, never as a row of blank or zero values.
const UNREADABLE = 'quarantine record unreadable — still blocking'

const unreadableOnly = {
  quarantined: [{
    repository: 'widget', task_issue: 40, readable: false, blocker_repo: '',
    blocker_issue: 0, fingerprint: '', created_at: '', blocker_open: null,
  }],
  fingerprints: [],
}

it('an unreadable record renders as an explicit row with a Retry button', async () => {
  server.use(http.get('/api/failures', () => HttpResponse.json(unreadableOnly)))
  renderWithProviders(<FailuresPage />)
  const row = await screen.findByRole('group', { name: 'widget#40' })
  expect(row).toHaveTextContent(UNREADABLE)
  expect(within(row).getByRole('button', { name: 'Retry' })).toBeEnabled()
  expect(row).not.toHaveTextContent('blocker #0')
  expect(row).not.toHaveTextContent('blocker state unknown')
  expect(row).not.toHaveTextContent(/ago|just now/)
})

it('Retry on an unreadable row calls onRetry with its task and is disabled while busy', async () => {
  const onRetry = vi.fn()
  const { rerender } = render(<FailureList failures={unreadableOnly} onRetry={onRetry} />)
  const row = screen.getByRole('group', { name: 'widget#40' })
  expect(row).toHaveTextContent(UNREADABLE)
  await userEvent.click(within(row).getByRole('button', { name: 'Retry' }))
  expect(onRetry).toHaveBeenCalledWith('widget', 40)
  rerender(<FailureList failures={unreadableOnly} onRetry={onRetry} busy={new Set(['widget#40'])} />)
  expect(within(screen.getByRole('group', { name: 'widget#40' })).getByRole('button', { name: 'Retry' })).toBeDisabled()
})

it('retrying an unreadable row posts its intent, disables it in flight and shows its error in the row', async () => {
  const g = gate()
  let posted = false
  server.use(
    http.get('/api/failures', () => HttpResponse.json(unreadableOnly)),
    http.post('/api/task/widget/40/retry', async () => {
      posted = true
      await g.held
      return HttpResponse.json(
        { detail: 'issue widget/40 is not quarantined' }, { status: 404 },
      )
    }),
  )
  renderWithProviders(<FailuresPage />)
  const row = await screen.findByRole('group', { name: 'widget#40' })
  expect(row).toHaveTextContent(UNREADABLE)
  const retry = within(row).getByRole('button', { name: 'Retry' })
  await userEvent.click(retry)
  await waitFor(() => expect(retry).toBeDisabled())
  expect(posted).toBe(true)
  g.release()
  await waitFor(() =>
    expect(within(row).getByText('issue widget/40 is not quarantined')).toBeInTheDocument(),
  )
  expect(retry).toBeEnabled()
})

it('the default fixtures render the readable row as before beside the unreadable one', async () => {
  renderWithProviders(<FailuresPage />)
  const readable = await screen.findByRole('group', { name: 'widget#38' })
  await waitFor(() => expect(readable).toHaveTextContent('pytest::test_auth_flow'))
  expect(readable).toHaveTextContent('blocker #39 (jesdi/widget)')
  expect(within(readable).getByText('blocker open')).toBeInTheDocument()
  expect(readable).toHaveTextContent(/\d+[smhd] ago|just now/)
  expect(readable).not.toHaveTextContent(/unreadable/)
  expect(screen.getByRole('group', { name: 'widget#40' })).toHaveTextContent(UNREADABLE)
})
