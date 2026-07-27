import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { budgetUnavailable, pendingReplyIntent } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { BoardPage } from '../BoardPage'

beforeEach(() => server.use(...defaultHandlers))

it('renders all nine columns with cards and capacity', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('column-parked')).toBeInTheDocument(),
  )
  expect(screen.getAllByTestId(/^column-/)).toHaveLength(9)
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
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
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
