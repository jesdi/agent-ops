import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

const job = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id, name, description: `${name} does its work`, state: 'active',
  schedule: 'every 5 min', last_run: null, next_run: null, read_only: false,
  recent_runs: [], upcoming: [], running: false, ...extra,
})

// `posts` records every action the page sends; `current` is what the next GET returns.
function serve(current: () => Record<string, unknown>) {
  const posts: string[] = []
  server.use(
    http.get('/api/services', () => HttpResponse.json({
      now: '2026-11-24T10:12:00+00:00', managed: [current()], read_only: [],
    })),
    http.post('/api/services/:job/:verb', ({ params }) => {
      posts.push(`${params.job}/${params.verb}`)
      return HttpResponse.json({})
    }),
    http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
  )
  return posts
}

describe('services page actions: run now and stop run', () => {
  it('Run now on an active job posts to its run endpoint', async () => {
    const posts = serve(() => job('agent-ops-worktree-sweep', 'Stale worktree sweep'))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Stale worktree sweep'))

    await userEvent.click(await screen.findByRole('button', { name: /run now/i }))

    await waitFor(() => expect(posts).toEqual(['agent-ops-worktree-sweep/run']))
  })

  it('Run now is offered on a paused job and runs it', async () => {
    const posts = serve(() => job('agent-ops-triage', 'Backlog triage', { state: 'paused' }))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Backlog triage'))

    await userEvent.click(await screen.findByRole('button', { name: /run now/i }))

    await waitFor(() => expect(posts).toEqual(['agent-ops-triage/run']))
  })

  it('shows the job as running once Run now has started it', async () => {
    let state = 'active'
    serve(() => job('agent-ops-worktree-sweep', 'Stale worktree sweep', { state, running: state === 'running' }))
    server.use(http.post('/api/services/:job/run', () => { state = 'running'; return HttpResponse.json({}) }))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Stale worktree sweep'))

    await userEvent.click(await screen.findByRole('button', { name: /run now/i }))

    expect(await screen.findByText('running')).toBeInTheDocument()
  })

  it('Stop run on a running job posts to its stop endpoint with no confirmation', async () => {
    const confirm = vi.spyOn(window, 'confirm')
    const posts = serve(() => job('agent-ops-dispatcher', 'Dispatcher pass', { state: 'running', running: true }))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Dispatcher pass'))

    await userEvent.click(await screen.findByRole('button', { name: /stop run/i }))

    await waitFor(() => expect(posts).toEqual(['agent-ops-dispatcher/stop']))
    expect(confirm).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('shows the newest run as stopped by the operator after Stop run', async () => {
    let stopped = false
    serve(() => job('agent-ops-dispatcher', 'Dispatcher pass', stopped
      ? { recent_runs: [{ at: '2026-11-24T10:10:00+00:00', duration_seconds: 0, result: 'stopped' }] }
      : { state: 'running', running: true, recent_runs: [{ at: '2026-11-24T10:10:00+00:00', duration_seconds: null, result: null }] }))
    server.use(http.post('/api/services/:job/stop', () => { stopped = true; return HttpResponse.json({}) }))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Dispatcher pass'))

    await userEvent.click(await screen.findByRole('button', { name: /stop run/i }))

    expect(await screen.findByText(/stopped/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /stop run/i })).toBeNull()
  })

  it('a paused job run by hand stays paused with Resume, shows the run and offers Stop run', async () => {
    const posts = serve(() => job('agent-ops-triage', 'Backlog triage', { state: 'paused', running: true }))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Backlog triage'))

    const detail = await screen.findByRole('region', { name: 'Job detail' })
    expect(within(detail).getByText('paused · running now')).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: /^resume$/i })).toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: /^pause$/i })).toBeNull()
    await userEvent.click(within(detail).getByRole('button', { name: /stop run/i }))
    await waitFor(() => expect(posts).toEqual(['agent-ops-triage/stop']))
  })

  it('offers Run now and no Stop run on an active job that is not running', async () => {
    serve(() => job('agent-ops-digest', 'Daily digest'))
    renderWithProviders(<ServicesPage />)
    await userEvent.click(await screen.findByText('Daily digest'))

    expect(await screen.findByRole('button', { name: /run now/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /stop run/i })).toBeNull()
  })
})
