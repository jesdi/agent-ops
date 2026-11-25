import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

const job = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id, name, description: `${name} does its work`, state: 'active',
  schedule: 'every 5 min', last_run: null, next_run: null, read_only: false,
  recent_runs: [], upcoming: [], change: null, ...extra,
})

function serve(...managed: ReturnType<typeof job>[]) {
  server.use(
    http.get('/api/services', () => HttpResponse.json({
      now: '2026-11-24T10:12:00+00:00', managed, read_only: [],
    })),
    http.put('/api/services/:job/schedule', () =>
      HttpResponse.json({ detail: 'every must be a whole number of at least 1' }, { status: 422 })),
    http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
  )
}

describe('services page: selecting another job', () => {
  it('starts the other job with an empty editor and none of the first job\'s errors', async () => {
    serve(job('agent-ops-dispatcher', 'Dispatcher pass'), job('agent-ops-sweep', 'Stale worktree sweep'))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Dispatcher pass'))
    await userEvent.type(screen.getByRole('textbox', { name: 'Minutes' }), '0')
    await userEvent.click(screen.getByRole('button', { name: 'Save schedule' }))
    expect(await screen.findByText(/whole number/)).toBeInTheDocument()

    await userEvent.click(screen.getByText('Stale worktree sweep'))

    expect(screen.getByRole('heading', { name: 'Stale worktree sweep' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Minutes' })).toHaveValue('')
    expect(screen.queryByText(/whole number/)).not.toBeInTheDocument()
  })
})
