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
