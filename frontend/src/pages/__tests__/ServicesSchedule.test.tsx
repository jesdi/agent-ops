import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

const PR_URL = 'https://github.com/jesdi/agent-ops-infra/pull/12'

type Change = { state: string; pr: number | null; url: string | null } | null

const job = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id, name, description: `${name} does its work`, state: 'active',
  schedule: 'every 5 min', last_run: null, next_run: null, read_only: false,
  recent_runs: [], upcoming: [], change: null as Change, running: false, ...extra,
})

// `puts` records every schedule saved as "<job>: <json body>". A save the server accepts makes
// the next GET show the job with "applying change · PR #12" and its old schedule; `refuse` makes
// the server answer with that status and message instead.
function serve(initial: ReturnType<typeof job>, refuse?: { status: number; detail: string }) {
  let current = initial
  let paused = initial.state === 'paused'
  const puts: string[] = []
  const posts: string[] = []
  server.use(
    http.get('/api/services', () => HttpResponse.json({
      now: '2026-11-24T10:12:00+00:00', managed: [current], read_only: [],
    })),
    http.put('/api/services/:job/schedule', async ({ params, request }) => {
      puts.push(`${params.job}: ${JSON.stringify(await request.json())}`)
      if (refuse) return HttpResponse.json({ detail: refuse.detail }, { status: refuse.status })
      current = { ...current, change: { state: 'applying', pr: 12, url: PR_URL } }
      return HttpResponse.json({})
    }),
    http.post('/api/services/:job/:verb', ({ params }) => {
      posts.push(`${params.job}/${params.verb}`)
      if (params.verb === 'pause') paused = true
      if (params.verb === 'resume') paused = false
      const running = params.verb === 'run'
      current = { ...current, running, state: running ? 'running' : paused ? 'paused' : 'active' }
      return HttpResponse.json({})
    }),
    http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
  )
  return { puts, posts }
}

async function open(name: string) {
  renderWithProviders(<ServicesPage />)
  await userEvent.click(await screen.findByText(name))
}

afterEach(() => vi.restoreAllMocks())

// The PR number may be a link inside the applying-change message.
async function applying() {
  expect(await screen.findByText(/applying change/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /#12/ })).toHaveAttribute('href', PR_URL)
}

const kind = () => screen.findByRole('combobox', { name: /schedule type/i })
const save = () => screen.findByRole('button', { name: /save schedule/i })

async function fill(label: RegExp, value: string) {
  const field = await screen.findByLabelText(label)
  await userEvent.clear(field)
  await userEvent.type(field, value)
}

describe('services page: the schedule editor', () => {
  it('saves "every 10 minutes", then shows "applying change · PR #12" on the old schedule with the editor locked', async () => {
    const { puts } = serve(job('agent-ops-dispatcher', 'Dispatcher pass'))
    await open('Dispatcher pass')

    await userEvent.selectOptions(await kind(), 'Every N minutes')
    await fill(/minutes/i, '10')
    await userEvent.click(await save())

    await waitFor(() => expect(puts).toEqual(['agent-ops-dispatcher: {"every":10}']))
    await applying()
    expect(screen.getByText('every 5 min')).toBeInTheDocument()
    expect(await save()).toBeDisabled()
  })

  it('saves "daily at 05:00 Europe/Madrid"', async () => {
    const { puts } = serve(job('agent-ops-triage', 'Backlog triage', { schedule: 'daily at 04:00 Europe/Madrid' }))
    await open('Backlog triage')

    await userEvent.selectOptions(await kind(), 'Daily at')
    await fill(/^time$/i, '05:00')
    await fill(/timezone/i, 'Europe/Madrid')
    await userEvent.click(await save())

    await waitFor(() => expect(puts).toEqual(['agent-ops-triage: {"daily":"05:00","tz":"Europe/Madrid"}']))
    await applying()
  })

  it('saves a custom systemd calendar expression', async () => {
    const { puts } = serve(job('agent-ops-digest', 'Daily digest', { schedule: 'daily at 08:00 UTC' }))
    await open('Daily digest')

    await userEvent.selectOptions(await kind(), 'Custom')
    await fill(/expression/i, 'Mon..Fri *-*-* 09:00')
    await userEvent.click(await save())

    await waitFor(() => expect(puts).toEqual(['agent-ops-digest: {"calendar":"Mon..Fri *-*-* 09:00"}']))
    await applying()
  })

  it('shows the message of a refused schedule and no "applying change"', async () => {
    serve(job('agent-ops-onboard', 'Onboarding pass', { schedule: 'every 1 min' }),
      { status: 422, detail: 'onboard: every must be a whole number of at least 1' })
    await open('Onboarding pass')

    await userEvent.selectOptions(await kind(), 'Every N minutes')
    await fill(/minutes/i, '0')
    await userEvent.click(await save())

    expect(await screen.findByText(/every must be a whole number of at least 1/)).toBeInTheDocument()
    expect(screen.queryByText(/applying change/)).not.toBeInTheDocument()
    expect(screen.getByText('every 1 min')).toBeInTheDocument()
  })

  it('shows that the deploy sync is paused when the save is refused for it', async () => {
    serve(job('agent-ops-dispatcher', 'Dispatcher pass'),
      { status: 409, detail: 'The deploy sync is paused: a schedule change cannot be applied.' })
    await open('Dispatcher pass')

    await userEvent.selectOptions(await kind(), 'Every N minutes')
    await fill(/minutes/i, '10')
    await userEvent.click(await save())

    expect(await screen.findByText(/deploy sync is paused/i)).toBeInTheDocument()
    expect(screen.queryByText(/applying change/)).not.toBeInTheDocument()
  })

  it('locks the editor while a change applies, but Pause, Run now and Stop run still post', async () => {
    const { posts } = serve(job('agent-ops-dispatcher', 'Dispatcher pass', {
      change: { state: 'applying', pr: 12, url: PR_URL },
    }))
    await open('Dispatcher pass')

    expect(await save()).toBeDisabled()
    await applying()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }))
    await waitFor(() => expect(posts).toEqual(['agent-ops-dispatcher/pause']))
    expect(confirm).toHaveBeenCalled()
    expect(await save()).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: /^run now$/i }))
    await userEvent.click(await screen.findByRole('button', { name: /^stop run$/i }))
    await waitFor(() => expect(posts).toEqual(['agent-ops-dispatcher/pause', 'agent-ops-dispatcher/run', 'agent-ops-dispatcher/stop']))
    expect(await save()).toBeDisabled()
    expect(screen.getByRole('button', { name: /^resume$/i })).toBeEnabled()
  })

  it('unlocks the editor once the change has applied: no "applying change" and Save is enabled', async () => {
    serve(job('agent-ops-dispatcher', 'Dispatcher pass', { schedule: 'every 10 min' }))
    await open('Dispatcher pass')

    expect(await screen.findByText('every 10 min')).toBeInTheDocument()
    expect(await save()).toBeEnabled()
    expect(screen.queryByText(/applying change/)).not.toBeInTheDocument()
  })

  it('a failed change shows "change failed", its link and Retry, and leaves the editor unlocked', async () => {
    serve(job('agent-ops-dispatcher', 'Dispatcher pass', { change: { state: 'failed', pr: 12, url: PR_URL } }))
    await open('Dispatcher pass')

    expect(await within(await screen.findByRole('region', { name: 'Job detail' })).findByText(/change failed/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /#12/ })).toHaveAttribute('href', PR_URL)
    expect(screen.getByRole('button', { name: /^retry$/i })).toBeInTheDocument()
    expect(screen.getByText('every 5 min')).toBeInTheDocument()
    expect(await save()).toBeEnabled()
  })
})
