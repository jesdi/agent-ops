import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

const PR_URL = 'https://github.com/jesdi/agent-ops-infra/pull/12'

type Change = { state: string; pr: number | null; url: string | null } | null

const job = (extra: { change?: Change } = {}) => ({
  id: 'agent-ops-digest', name: 'Daily digest', description: 'Daily digest does its work',
  state: 'paused', schedule: 'daily at 08:00 UTC', last_run: null, next_run: null,
  read_only: false, recent_runs: [], upcoming: [], change: null as Change, ...extra,
})

// `posts` records every action sent; the retry clears the failed change on the next GET.
function serve(initial: ReturnType<typeof job>) {
  let current = initial
  const posts: string[] = []
  server.use(
    http.get('/api/services', () => HttpResponse.json({
      now: '2026-11-24T10:12:00+00:00', managed: [current], read_only: [],
    })),
    http.post('/api/services/:job/:verb', ({ params }) => {
      posts.push(`${params.job}/${params.verb}`)
      if (params.verb === 'retry') current = { ...current, change: null }
      return HttpResponse.json({})
    }),
    http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
  )
  return posts
}

async function open() {
  renderWithProviders(<ServicesPage />)
  await userEvent.click(await screen.findByText('Daily digest'))
}

describe('services page: a change that failed to record', () => {
  it('shows "change failed" with a link to the pull request and a Retry action', async () => {
    serve(job({ change: { state: 'failed', pr: 12, url: PR_URL } }))
    await open()

    expect(await screen.findByText(/change failed/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /#12|pull request/i })).toHaveAttribute('href', PR_URL)
    expect(screen.getByRole('button', { name: /^retry$/i })).toBeInTheDocument()
  })

  it('with no pull request it shows "change failed" and Retry, but no link', async () => {
    serve(job({ change: { state: 'failed', pr: null, url: null } }))
    await open()

    expect(await screen.findByText(/change failed/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^retry$/i })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /#\d+|pull request/i })).not.toBeInTheDocument()
  })

  it('Retry posts to the job\'s retry endpoint and "change failed" goes away', async () => {
    const posts = serve(job({ change: { state: 'failed', pr: null, url: null } }))
    await open()

    await userEvent.click(await screen.findByRole('button', { name: /^retry$/i }))

    await waitFor(() => expect(posts).toEqual(['agent-ops-digest/retry']))
    await waitFor(() => expect(screen.queryByText(/change failed/i)).not.toBeInTheDocument())
  })
})
