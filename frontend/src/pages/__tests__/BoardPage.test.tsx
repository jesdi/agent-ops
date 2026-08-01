import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { board as fx_board, budgetUnavailable, pendingReplyIntent } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { BoardPage } from '../BoardPage'

beforeEach(() => server.use(...defaultHandlers))

it('renders all ten columns with cards and capacity', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('column-parked')).toBeInTheDocument(),
  )
  expect(screen.getAllByTestId(/^column-/)).toHaveLength(10)
  expect(screen.getByText('Fix login redirect')).toBeInTheDocument()
  expect(screen.getByText('Add CSV export')).toBeInTheDocument()
  expect(screen.getByText(/2\/3 active/)).toBeInTheDocument()
})

it('AWKWARD: budget source unavailable shows the consequence, not a gauge', async () => {
  server.use(http.get('/api/budget', () => HttpResponse.json(budgetUnavailable)))
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(
      screen.getByText('usage unknown — dispatcher will not spawn'),
    ).toBeInTheDocument(),
  )
  expect(
    screen.queryByRole('progressbar', { name: 'usage budget utilization' }),
  ).not.toBeInTheDocument()
})

it('AWKWARD: a pending intent renders a badge on the affected card', async () => {
  server.use(
    http.get('/api/pending-intents', () => HttpResponse.json(pendingReplyIntent)),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('pending-badge')).toBeInTheDocument(),
  )
  expect(screen.getByTestId('card-42')).toContainElement(
    screen.getByTestId('pending-badge'),
  )
  expect(screen.getByText('pending: reply')).toBeInTheDocument()
})

it('several pending intents on one issue all render — none silently dropped', async () => {
  server.use(
    http.get('/api/pending-intents', () =>
      HttpResponse.json({
        intents: [
          { action: 'reply', issue: 42, actor: 'dev@localhost',
            created_at: '2026-07-25T11:58:00Z' },
          { action: 'kill', issue: 42, actor: 'dev@localhost',
            created_at: '2026-07-25T11:59:00Z' },
        ],
      }),
    ),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getAllByTestId('pending-badge')).toHaveLength(2),
  )
  expect(screen.getByText('pending: reply')).toBeInTheDocument()
  expect(screen.getByText('pending: kill')).toBeInTheDocument()
})

it('a failing /api/budget states the gap instead of silently dropping the gauge', async () => {
  server.use(
    http.get('/api/budget', () =>
      HttpResponse.json({ detail: 'budget source exploded' }, { status: 500 }),
    ),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('budget-error')).toBeInTheDocument(),
  )
  expect(screen.getByTestId('budget-error')).toHaveTextContent('usage unknown')
  expect(
    screen.queryByRole('progressbar', { name: 'usage budget utilization' }),
  ).not.toBeInTheDocument()
})

it('AWKWARD: two parked cards, only the login-parked one holds a unit', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('card-45')).toBeInTheDocument(),
  )
  // login-parked: still running, still consuming
  expect(screen.getByTestId('card-45')).toHaveTextContent('holding a capacity unit')
  // CI-parked: container stopped, unit released
  expect(screen.getByTestId('card-46')).not.toHaveTextContent('holding a capacity unit')
})

it('the accented cards reconcile with the header meter', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByRole('progressbar', { name: 'capacity units in use' }))
      .toBeInTheDocument(),
  )
  expect(screen.getAllByText('holding a capacity unit')).toHaveLength(2)
  expect(screen.getAllByTestId('cap-pip-filled')).toHaveLength(2)
  expect(screen.getByText(/2\/3 active/)).toBeInTheDocument()
})

test('queued column renders ghost cards in rank order with count and stale hint', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    upcoming: [
      { number: 73, target: 'jesdi/widget', title: 'Ship dark mode', url: 'u', score: 8.5, boost: 0 },
      { number: 74, target: 'jesdi/widget', title: 'Fix flaky test', url: 'u', score: 3.5, boost: 0 },
    ],
    upcoming_stale: true,
    next_claim: { ...fx_board.next_claim, verdict: 'will-claim', next_issue: 73 },
  })))
  renderWithProviders(<BoardPage />)
  const queued = await screen.findByTestId('column-queued')
  const ghosts = within(queued).getAllByTestId(/^ghost-/)
  expect(ghosts.map((g) => g.dataset.testid)).toEqual(['ghost-73', 'ghost-74'])
  expect(within(queued).getByText('2')).toBeInTheDocument()  // header count
  expect(within(queued).getByText(/stale/)).toBeInTheDocument()
  expect(within(ghosts[0]!).getByText('next')).toBeInTheDocument()
})

test('header shows next-claim line and median cycle', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    median_cycle_seconds: 7200,
    next_claim: { verdict: 'capacity-full', next_pass_eta: new Date(Date.now() + 300_000).toISOString(), next_issue: 0, next_target: '', minutes_to_reset: 0 },
  })))
  renderWithProviders(<BoardPage />)
  expect(await screen.findByTestId('next-claim')).toHaveTextContent(/capacity full/i)
  expect(screen.getByText(/≈2h per task/)).toBeInTheDocument()
})
