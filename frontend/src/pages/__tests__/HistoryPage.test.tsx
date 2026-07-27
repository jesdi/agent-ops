import { screen, waitFor } from '@testing-library/react'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { renderWithProviders } from '../../test/render'
import { HistoryPage } from '../HistoryPage'

beforeEach(() => server.use(...defaultHandlers))

it('renders the event timeline newest-first with actor and detail', async () => {
  renderWithProviders(<HistoryPage />)
  await waitFor(() =>
    expect(screen.getByText('stage-started')).toBeInTheDocument(),
  )
  const rows = screen.getAllByTestId('timeline-row')
  expect(rows).toHaveLength(2)
  expect(rows[0]!.textContent).toContain('stage-started')
  expect(rows[1]!.textContent).toContain('parked')
  expect(rows[1]!.textContent).toContain('question')
})
