import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ServicesPage } from '../ServicesPage'

const job = (state: string) => ({
  id: 'agent-ops-digest', name: 'Daily digest', description: 'Daily digest does its work', state,
  schedule: 'daily at 08:00 UTC', last_run: null, next_run: null, read_only: false,
  recent_runs: [], upcoming: [], change: null, warning: null, running: state === 'running',
})

function serve(state: string) {
  const posts: string[] = []
  server.use(
    http.get('/api/services', () => HttpResponse.json({
      now: '2026-11-24T10:12:00+00:00', config_installed: false, managed: [job(state)], read_only: [],
    })),
    http.post('/api/services/:job/:verb', ({ params }) => {
      posts.push(`${params.job}/${params.verb}`)
      return HttpResponse.json({})
    }),
    http.get('/api/exams', () => HttpResponse.json({ exams: [] })),
  )
  return posts
}

async function open(state: string) {
  const posts = serve(state)
  renderWithProviders(<ServicesPage />)
  await userEvent.click(await screen.findByText('Daily digest'))
  return posts
}

describe('services page: no services config installed', () => {
  it('says so and disables Pause and Save schedule, while Run now still posts', async () => {
    const posts = await open('active')

    expect(await screen.findByText(/services config is not installed on the box yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^pause$/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /save schedule/i })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: /schedule type/i })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: /^run now$/i }))
    await waitFor(() => expect(posts).toEqual(['agent-ops-digest/run']))
  })

  it('disables Resume on a paused job', async () => {
    await open('paused')

    expect(await screen.findByRole('button', { name: /^resume$/i })).toBeDisabled()
  })

  it('keeps Stop run on a running job', async () => {
    const posts = await open('running')

    await userEvent.click(await screen.findByRole('button', { name: /^stop run$/i }))
    await waitFor(() => expect(posts).toEqual(['agent-ops-digest/stop']))
  })
})
