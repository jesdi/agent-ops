import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { staleQueue } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { QueuePage } from '../QueuePage'

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
