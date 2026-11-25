import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

const job = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id, name, description: `${name} does its work`, state: 'active',
  schedule: 'every 5 min', last_run: null, next_run: null, read_only: false,
  recent_runs: [], upcoming: [], ...extra,
})

// `posts` records every action sent; pause/resume flip the state the next GET returns.
function serve(initial: ReturnType<typeof job>, { fail = false } = {}) {
  let state = initial.state
  const posts: string[] = []
  server.use(
    http.get('/api/services', () => HttpResponse.json({
      now: '2026-11-24T10:12:00+00:00', managed: [{ ...initial, state }], read_only: [],
    })),
    http.post('/api/services/:job/:verb', ({ params }) => {
      posts.push(`${params.job}/${params.verb}`)
      if (fail) return HttpResponse.json({ detail: 'systemctl failed' }, { status: 500 })
      state = params.verb === 'pause' ? 'paused' : 'active'
      return HttpResponse.json({})
    }),
    http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
  )
  return posts
}

async function open(name: string) {
  renderWithProviders(<ServicesPage />)
  await userEvent.click(await screen.findByText(name))
}

afterEach(() => vi.restoreAllMocks())

describe('services page: pause and resume', () => {
  it('Claude login keepalive: Pause asks with the consequence, and confirming pauses it', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const posts = serve(job('agent-ops-keepalive', 'Claude login keepalive'))
    await open('Claude login keepalive')

    await userEvent.click(await screen.findByRole('button', { name: /^pause$/i }))

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('The Claude login expires if this stays paused.'))
    await waitFor(() => expect(posts).toEqual(['agent-ops-keepalive/pause']))
    expect(await within(await screen.findByRole('region', { name: 'Job detail' })).findByText('paused')).toBeInTheDocument()
  })

  it.each([
    ['agent-ops-codex-keepalive', 'Codex login keepalive', /Codex login expires/i],
    ['agent-ops-dispatcher', 'Dispatcher pass', /no tasks are claimed or driven/i],
    ['agent-ops-update', 'Deploy sync', /deploys and config changes stop being applied/i],
  ])('%s: the confirmation states its consequence', async (id, name, consequence) => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    serve(job(id, name))
    await open(name)

    await userEvent.click(await screen.findByRole('button', { name: /^pause$/i }))

    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(consequence))
  })

  it('declining the confirmation leaves the deploy sync active and sends no request', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const posts = serve(job('agent-ops-update', 'Deploy sync'))
    await open('Deploy sync')

    await userEvent.click(await screen.findByRole('button', { name: /^pause$/i }))

    expect(confirm).toHaveBeenCalled()
    expect(posts).toEqual([])
    expect(screen.getByText('active')).toBeInTheDocument()
  })

  it('the stale worktree sweep is paused with no confirmation', async () => {
    const confirm = vi.spyOn(window, 'confirm')
    const posts = serve(job('agent-ops-worktree-sweep', 'Stale worktree sweep'))
    await open('Stale worktree sweep')

    await userEvent.click(await screen.findByRole('button', { name: /^pause$/i }))

    await waitFor(() => expect(posts).toEqual(['agent-ops-worktree-sweep/pause']))
    expect(confirm).not.toHaveBeenCalled()
    expect(await within(await screen.findByRole('region', { name: 'Job detail' })).findByText('paused')).toBeInTheDocument()
  })

  it('Resume on a paused job posts to its resume endpoint and shows it active', async () => {
    const posts = serve(job('agent-ops-digest', 'Daily digest', { state: 'paused' }))
    await open('Daily digest')

    await userEvent.click(await screen.findByRole('button', { name: /^resume$/i }))

    await waitFor(() => expect(posts).toEqual(['agent-ops-digest/resume']))
    expect(await screen.findByText('active')).toBeInTheDocument()
  })

  it('a failed Pause shows an error and the job still shows active', async () => {
    serve(job('agent-ops-digest', 'Daily digest'), { fail: true })
    await open('Daily digest')

    await userEvent.click(await screen.findByRole('button', { name: /^pause$/i }))

    expect(await screen.findByText(/systemctl failed/)).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
  })
})
