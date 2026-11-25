import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

// A job's `warning` is the message the API gives when the box differs from the services config.
const WARNING = 'The box differs from the services config.'

const job = (id: string, name: string, warning: string | null) => ({
  id, name, description: `${name} does its work`, state: 'paused', schedule: 'daily at 03:00 UTC',
  last_run: null, next_run: null, read_only: false, recent_runs: [], upcoming: [], change: null, warning,
})

const serve = () => server.use(
  http.get('/api/services', () => HttpResponse.json({
    now: '2026-11-24T10:12:00+00:00',
    managed: [job('agent-ops-sweep', 'Stale worktree sweep', WARNING), job('agent-ops-digest', 'Daily digest', null)],
    read_only: [],
  })),
  http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
)

describe('services page: a job that differs from the config', () => {
  it('shows paused with the warning for a flagged job', async () => {
    serve()
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Stale worktree sweep'))

    expect(await screen.findByText(WARNING)).toBeInTheDocument()
    expect(within(await screen.findByRole('region', { name: 'Job detail' })).getByText('paused')).toBeInTheDocument()
  })

  it('shows no warning once a paused job that is not flagged is selected', async () => {
    serve()
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Stale worktree sweep'))
    expect(await screen.findByText(WARNING)).toBeInTheDocument()

    await userEvent.click(screen.getByText('Daily digest'))

    expect(await screen.findByText('Daily digest does its work')).toBeInTheDocument()
    expect(screen.queryByText(WARNING)).not.toBeInTheDocument()
  })
})
