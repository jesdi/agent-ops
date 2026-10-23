import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { renderWithProviders } from '../../test/render'
import { BoardPage } from '../../pages/BoardPage'
import { board as fx_board, inProgressCard } from '../../test/fixtures'

const analytics = { name: 'analytics', state: 'enabled', repositories: ['example/analytics'], pending: null }
const projects = (...p: unknown[]) => http.get('/api/projects', () => HttpResponse.json({ projects: p }))

beforeEach(() => server.use(...defaultHandlers))

it('an empty paused project has its own tab; pause is confirmed before it is requested', async () => {
  let body: unknown = null
  server.use(
    projects(analytics, { name: 'shop', state: 'paused', repositories: ['example/shop-frontend', 'example/shop-backend'], pending: null }),
    http.post('/api/projects/analytics/pause', async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ operation: 'ctl-1' }, { status: 202 })
    }),
  )
  renderWithProviders(<BoardPage />)
  await userEvent.click(await screen.findByRole('tab', { name: 'shop' }))
  expect(screen.getByText('example/shop-backend')).toBeInTheDocument()
  expect(screen.getByText('paused')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('tab', { name: 'analytics' }))
  await userEvent.click(screen.getByRole('button', { name: 'pause' }))
  expect(body).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Confirm pause' }))
  await waitFor(() => expect(body).toEqual({ confirm: true }))
})

it('shows a failed change as pending beside the unchanged effective state', async () => {
  server.use(projects({
    ...analytics, state: 'paused',
    pending: { operation: 'ctl-2', requested: 'enabled', phase: 'blocked', blocker: 'check unit-tests failing', pr: { number: 7, url: 'https://x/7' } },
  }))
  renderWithProviders(<BoardPage />)
  await userEvent.click(await screen.findByRole('tab', { name: 'analytics' }))
  expect(screen.getByText('paused')).toBeInTheDocument()
  expect(screen.getByTestId('project-pending')).toHaveTextContent('Pending: enabled (blocked) — check unit-tests failing')
  expect(screen.getByRole('button', { name: 'enable' })).toBeDisabled()
})

it('a project tab scopes cards and queued ghosts to that project and its queue actions name it', async () => {
  const card = (target: string, issue: number) => ({ ...inProgressCard, target, issue, title: `${target} task` })
  const ghost = (target: string, number: number) =>
    ({ number, target, repo: `example/${target}`, title: `${target} queued`, url: 'u', score: 5, boost: 0 })
  let boosted: unknown = null
  server.use(
    http.get('/api/board', () => HttpResponse.json({
      ...fx_board,
      columns: fx_board.columns.map((c) =>
        c.key === 'in-progress' ? { ...c, cards: [card('alpha', 1), card('beta', 2)] }
          : c.key === 'queued' ? { ...c, ghosts: [ghost('alpha', 11), ghost('beta', 12)] } : c),
    })),
    projects(
      { ...analytics, name: 'alpha', repositories: ['example/alpha'] },
      { ...analytics, name: 'beta', repositories: ['example/beta'] },
    ),
    http.post('/api/queue/boost', async ({ request }) => {
      boosted = await request.json()
      return HttpResponse.json({})
    }),
  )
  renderWithProviders(<BoardPage />)
  expect(await screen.findByText('alpha queued')).toBeInTheDocument()
  expect(screen.getByText('beta queued')).toBeInTheDocument()
  await userEvent.click(await screen.findByRole('tab', { name: 'beta' }))
  expect(screen.getByText('beta task')).toBeInTheDocument()
  expect(screen.queryByText('alpha task')).not.toBeInTheDocument()
  const queued = screen.getByTestId('column-queued')
  expect(within(queued).queryByText('alpha queued')).not.toBeInTheDocument()
  await userEvent.click(within(queued).getByRole('button', { name: 'Details for beta#12' }))
  await userEvent.click(within(queued).getByRole('button', { name: 'Boost' }))
  await waitFor(() => expect(boosted).toMatchObject({ issue: 12, repo: 'example/beta' }))
  await userEvent.click(screen.getByRole('tab', { name: 'alpha' }))
  expect(screen.getByText('alpha task')).toBeInTheDocument()
  expect(screen.queryByText('beta task')).not.toBeInTheDocument()
  expect(screen.queryByText('beta queued')).not.toBeInTheDocument()
})

it('Next and Ready from each project tab submit that tab project\'s issue and repository', async () => {
  const ghost = (target: string, number: number) =>
    ({ number, target, repo: `example/${target}`, title: `${target} queued`, url: 'u', score: 5, boost: 0 })
  const posts: { path: string; body: unknown }[] = []
  const record = (path: string) => http.post(path, async ({ request }) => {
    posts.push({ path, body: await request.json() })
    return HttpResponse.json({})
  })
  server.use(
    http.get('/api/board', () => HttpResponse.json({
      ...fx_board,
      columns: fx_board.columns.map((c) =>
        c.key === 'queued' ? { ...c, ghosts: [ghost('alpha', 11), ghost('beta', 12)] } : c),
    })),
    projects(
      { ...analytics, name: 'alpha', repositories: ['example/alpha'] },
      { ...analytics, name: 'beta', repositories: ['example/beta'] },
    ),
    record('/api/queue/next'),
    record('/api/queue/ready'),
  )
  renderWithProviders(<BoardPage />)
  for (const [name, issue, button] of [['beta', 12, 'Next'], ['alpha', 11, 'Ready']] as const) {
    await userEvent.click(await screen.findByRole('tab', { name }))
    const queued = screen.getByTestId('column-queued')
    await userEvent.click(within(queued).getByRole('button', { name: `Details for ${name}#${issue}` }))
    await userEvent.click(within(queued).getByRole('button', { name: button }))
  }
  await waitFor(() => expect(posts).toHaveLength(2))
  expect(posts[0]).toMatchObject({ path: '/api/queue/next', body: { issue: 12, repo: 'example/beta' } })
  expect(posts[1]).toEqual({ path: '/api/queue/ready', body: { issue: 11, repo: 'example/alpha' } })
})

it('a refused control change is reported and the project stays unchanged', async () => {
  server.use(
    projects(analytics),
    http.post('/api/projects/analytics/pause', () =>
      HttpResponse.json({ detail: 'control service unavailable' }, { status: 503 })),
  )
  renderWithProviders(<BoardPage />)
  await userEvent.click(await screen.findByRole('tab', { name: 'analytics' }))
  await userEvent.click(screen.getByRole('button', { name: 'pause' }))
  await userEvent.click(screen.getByRole('button', { name: 'Confirm pause' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('control service unavailable')
  expect(screen.getByText('enabled')).toBeInTheDocument()
  expect(screen.queryByTestId('project-pending')).not.toBeInTheDocument()
})
