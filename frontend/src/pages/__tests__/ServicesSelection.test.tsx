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

describe('services page: next run', () => {
  it.each([
    ['2026-11-24T10:13:00+00:00', 'in 1 min'],
    ['2026-11-24T11:00:00+00:00', 'in 48 min'],
    ['2026-11-25T07:18:00+00:00', 'in 21h 6m'],
  ])('a next run at %s reads %s', async (next_run, words) => {
    serve(job('agent-ops-digest', 'Daily digest', { next_run }))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Daily digest'))
    expect(screen.getByText(words)).toBeInTheDocument()
  })
})

describe('services page: run times on other days', () => {
  it('dates a run or an upcoming run that is not today, and keeps today\'s as HH:MM', async () => {
    serve(job('agent-ops-digest', 'Daily digest', {
      last_run: { at: '2026-11-24T08:00:00+00:00', duration_seconds: 12 },
      recent_runs: [
        { at: '2026-11-24T08:00:00+00:00', duration_seconds: 12, result: 'success' },
        { at: '2026-11-23T08:00:00+00:00', duration_seconds: 11, result: 'success' },
      ],
      upcoming: ['2026-11-25T08:00:00+00:00'],
    }))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Daily digest'))
    expect(screen.getAllByText('08:00 UTC · 12 s')).toHaveLength(2)
    expect(screen.getByText('Nov 23 08:00 UTC · 11 s')).toBeInTheDocument()
    expect(screen.getByText('Nov 25 08:00 UTC')).toBeInTheDocument()
  })

  it('reads a next run under a minute away as in 1 min', async () => {
    serve(job('agent-ops-digest', 'Daily digest', { next_run: '2026-11-24T10:12:20+00:00' }))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Daily digest'))
    expect(screen.getByText('in 1 min')).toBeInTheDocument()
  })
})
