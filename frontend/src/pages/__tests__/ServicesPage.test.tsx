import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { AppShell } from '../../components/AppShell'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

const job = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id, name, description: `${name} does its work`, state: 'active',
  schedule: 'every 5 min', last_run: null, next_run: null, read_only: false, ...extra,
})

const dispatcher = job('agent-ops-dispatcher', 'Dispatcher pass', {
  last_run: { at: '2026-11-24T10:08:00+00:00', duration_seconds: 27 },
  next_run: '2026-11-24T10:13:00+00:00',
})
const triage = job('agent-ops-triage', 'Backlog triage', {
  state: 'paused', schedule: 'daily at 04:00 Europe/Madrid',
})
const refresh = job('secret-cache-refresh', 'Secret cache refresh', {
  read_only: true, schedule: '00:00, 12:00 UTC',
  last_run: { at: '2026-11-24T00:00:00+00:00', duration_seconds: 3 },
  next_run: '2026-11-24T12:00:00+00:00',
})

const serve = () => server.use(
  http.get('/api/services', () => HttpResponse.json({
    now: '2026-11-24T10:12:00+00:00', managed: [dispatcher, triage], read_only: [refresh],
  })),
  http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
)

const BUTTONS = /pause|run now|stop run|resume/i

describe('services page', () => {
  it('is reached from a Services navigation link and lists managed above read-only', async () => {
    serve()
    renderWithProviders(
      <AppShell><Routes><Route path="/services" element={<ServicesPage />} /></Routes></AppShell>,
    )
    await userEvent.click(screen.getByRole('link', { name: 'Services' }))

    const managed = await screen.findByText('Dispatcher pass')
    const readOnly = screen.getByText('Secret cache refresh')
    expect(managed.compareDocumentPosition(readOnly) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('Backlog triage')).toBeInTheDocument()
  })

  it('shows the dispatcher pass state, schedule, last run and next run', async () => {
    serve()
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Dispatcher pass'))

    expect(await screen.findByText('active')).toBeInTheDocument()
    expect(screen.getByText('every 5 min')).toBeInTheDocument()
    expect(screen.getByText('10:08 · 27 s')).toBeInTheDocument()
    expect(screen.getByText('in 1 min')).toBeInTheDocument()
  })

  it('shows a paused job that never ran as paused, never ran and no next run', async () => {
    serve()
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Backlog triage'))

    expect(await screen.findByText('paused')).toBeInTheDocument()
    expect(screen.getByText('never ran')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('marks a root job read-only and offers no actions or schedule editor', async () => {
    serve()
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Secret cache refresh'))

    expect(await screen.findByText(/read-only/i)).toBeInTheDocument()
    expect(screen.getByText('00:00, 12:00 UTC')).toBeInTheDocument()
    expect(screen.getByText('00:00 · 3 s')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryAllByRole('button', { name: BUTTONS })).toHaveLength(0))
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('spinbutton')).toBeNull()
  })
})

const iso = (h: number, m: number) => `2026-11-24T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00+00:00`
const serveDetail = (extra: Record<string, unknown>, id = 'agent-ops-dispatcher') => server.use(
  http.get('/api/services', () => HttpResponse.json({
    now: '2026-11-24T10:12:00+00:00',
    managed: [job(id, 'Subject job', { recent_runs: [], upcoming: [], ...extra })], read_only: [],
  })),
  http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
)

describe('services page detail: runs', () => {
  it('lists the five recent runs with result and duration, and the next three run times', async () => {
    serveDetail({
      last_run: { at: iso(10, 8), duration_seconds: 27 },
      next_run: iso(10, 13),
      recent_runs: [
        { at: iso(9, 55), duration_seconds: 41, result: 'success' },
        { at: iso(9, 50), duration_seconds: 52, result: 'failed' },
        { at: iso(9, 45), duration_seconds: 43, result: 'success' },
        { at: iso(9, 40), duration_seconds: 34, result: 'success' },
        { at: iso(9, 35), duration_seconds: 35, result: 'success' },
      ],
      upcoming: [iso(10, 13), iso(10, 18), iso(10, 23)],
    })
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Subject job'))

    expect(await screen.findByText('10:18')).toBeInTheDocument()
    expect(screen.getByText('10:23')).toBeInTheDocument()
    const runs = screen.getAllByText(/^09:\d\d · \d+ s/)
    expect(runs).toHaveLength(5)
    expect(runs.map((r) => r.textContent)).toEqual([
      expect.stringMatching(/^09:55 · 41 s/), expect.stringMatching(/^09:50 · 52 s/),
      expect.stringMatching(/^09:45 · 43 s/), expect.stringMatching(/^09:40 · 34 s/),
      expect.stringMatching(/^09:35 · 35 s/),
    ])
    expect(runs[1]?.closest('li, tr, div')).toHaveTextContent(/failed/i)
  })

  it('shows a daily 03:00 run in the upcoming list', async () => {
    serveDetail({
      schedule: 'daily at 03:00 UTC', next_run: '2026-11-25T03:00:00+00:00',
      upcoming: ['2026-11-25T03:00:00+00:00'],
    })
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Subject job'))

    expect(await screen.findByText('Nov 25 03:00')).toBeInTheDocument()
  })

  it('says "No runs recorded" for a job with no runs', async () => {
    serveDetail({ state: 'paused' })
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Subject job'))

    expect(await screen.findByText('No runs recorded')).toBeInTheDocument()
  })

  it('says no runs are scheduled for a paused job', async () => {
    serveDetail({ state: 'paused' })
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Subject job'))

    expect(await screen.findByText(/no runs (are )?scheduled/i)).toBeInTheDocument()
  })
})
