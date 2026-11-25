import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

const refresh = {
  id: 'secret-cache-refresh', name: 'Secret cache refresh', description: 'refreshes secrets',
  state: 'active', schedule: '00:00, 12:00 UTC', read_only: true,
  last_run: { at: '2026-11-24T00:00:00+00:00', duration_seconds: 3 },
  next_run: '2026-11-24T12:00:00+00:00', recent_runs: [], upcoming: ['2026-11-24T12:00:00+00:00'],
}

describe('a read-only job', () => {
  it('shows its upcoming runs and no recent runs', async () => {
    server.use(http.get('/api/services', () => HttpResponse.json({
      now: '2026-11-24T10:12:00+00:00', managed: [], read_only: [refresh],
    })))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByRole('button', { name: 'Secret cache refresh' }))
    expect(screen.getByText('Upcoming runs')).toBeInTheDocument()
    expect(screen.getByText('in 1h 48m')).toBeInTheDocument()
    expect(screen.queryByText('Recent runs')).not.toBeInTheDocument()
    expect(screen.queryByText('No runs recorded')).not.toBeInTheDocument()
  })
})
