import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { staleQueue } from '../../test/fixtures'
import type { QueueView } from '../../lib/api'
import { renderWithProviders } from '../../test/render'
import { QueuePage } from '../QueuePage'

const twoTargetQueue: QueueView = {
  targets: [
    {
      target: 'jesdi/alpha',
      as_of: '2026-07-25T11:55:00Z',
      stale: false,
      rows: [
        { number: 10, title: 'Alpha issue one', url: 'https://github.com/jesdi/alpha/issues/10',
          status: 'Ready', labels: [], blocked: false, score: 5.0, boost: 0, in_flight: false },
      ],
    },
    {
      target: 'jesdi/beta',
      as_of: '2026-07-25T11:55:00Z',
      stale: false,
      rows: [
        { number: 20, title: 'Beta issue one', url: 'https://github.com/jesdi/beta/issues/20',
          status: 'Ready', labels: [], blocked: false, score: 4.0, boost: 0, in_flight: false },
      ],
    },
  ],
}

beforeEach(() => server.use(...defaultHandlers))

it('renders ranked rows with score, boost, blocked, and in-flight markers', async () => {
  renderWithProviders(<QueuePage />)
  await waitFor(() =>
    expect(screen.getByText('Rate-limit webhooks')).toBeInTheDocument(),
  )
  expect(screen.getByText('8.5')).toBeInTheDocument()
  expect(screen.getByText('blocked')).toBeInTheDocument()
  expect(screen.getByText('in flight')).toBeInTheDocument()
})

it('AWKWARD: stale cache renders an "as of" banner instead of a blank queue', async () => {
  server.use(http.get('/api/queue', () => HttpResponse.json(staleQueue)))
  renderWithProviders(<QueuePage />)
  await waitFor(() =>
    expect(screen.getByTestId('stale-banner')).toBeInTheDocument(),
  )
  expect(screen.getByTestId('stale-banner').textContent).toMatch(/as of .*ago/)
  // rows still render — never a blank queue
  expect(screen.getByText('Rate-limit webhooks')).toBeInTheDocument()
})

it('boost posts {issue, amount: 1} then refetches the queue', async () => {
  let boosted: unknown = null
  server.use(
    http.post('/api/queue/boost', async ({ request }) => {
      boosted = await request.json()
      return HttpResponse.json({ ok: true, reason: 'boosted to band 2' })
    }),
  )
  renderWithProviders(<QueuePage />)
  await waitFor(() =>
    expect(screen.getByText('Rate-limit webhooks')).toBeInTheDocument(),
  )
  await userEvent.click(screen.getAllByRole('button', { name: 'Boost' })[0]!)
  await waitFor(() => expect(boosted).toEqual({ issue: 51, amount: 1 }))
})

it('a 422 shows the guard reason inline', async () => {
  server.use(
    http.post('/api/queue/next', () =>
      HttpResponse.json({ detail: 'issue 60 is blocked' }, { status: 422 }),
    ),
  )
  renderWithProviders(<QueuePage />)
  await waitFor(() =>
    expect(screen.getByText('Migrate to pydantic v2')).toBeInTheDocument(),
  )
  await userEvent.click(screen.getAllByRole('button', { name: 'Next' })[2]!)
  await waitFor(() =>
    expect(screen.getByText('issue 60 is blocked')).toBeInTheDocument(),
  )
})

it('422 error is scoped to the target that triggered it, not broadcast to all tables', async () => {
  server.use(
    http.get('/api/queue', () => HttpResponse.json(twoTargetQueue)),
    http.post('/api/queue/boost', () =>
      HttpResponse.json({ detail: 'alpha guard triggered' }, { status: 422 }),
    ),
  )
  renderWithProviders(<QueuePage />)
  await waitFor(() => expect(screen.getByText('Alpha issue one')).toBeInTheDocument())
  expect(screen.getByText('Beta issue one')).toBeInTheDocument()

  const alphaTable = screen.getByTestId('queue-jesdi/alpha')
  await userEvent.click(within(alphaTable).getAllByRole('button', { name: 'Boost' })[0]!)

  await waitFor(() =>
    expect(within(alphaTable).getByText('alpha guard triggered')).toBeInTheDocument(),
  )

  const betaTable = screen.getByTestId('queue-jesdi/beta')
  expect(within(betaTable).queryByText('alpha guard triggered')).not.toBeInTheDocument()
})
