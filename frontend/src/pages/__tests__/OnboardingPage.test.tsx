import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { NewOnboardingPage, OnboardingPage, OnboardingsPage } from '../OnboardingPage'

function renderNew() {
  return renderWithProviders(<Routes>
    <Route path="/onboarding/new" element={<NewOnboardingPage />} />
    <Route path="/onboarding/projects/:project" element={<h1>Onboarding received</h1>} />
  </Routes>, { route: '/onboarding/new' })
}

async function enterProject() {
  await userEvent.type(screen.getByLabelText('Project name'), 'analytics')
  await userEvent.type(screen.getByLabelText('GitHub board'), 'jesdi/4')
  await userEvent.type(screen.getByLabelText('GitHub repository'), 'jesdi/analytics')
  await userEvent.type(screen.getByLabelText('Gate command'), 'uv run pytest -q')
}

describe('onboarding worksheet', () => {
  it('submits project and repository commands, then opens the shared onboarding', async () => {
    let submitted: unknown
    server.use(http.post('/api/onboarding/analytics', async ({ request }) => {
      submitted = await request.json()
      return HttpResponse.json({ status: 'pending', intent: '100-add', request_revision: 'request-one' }, { status: 202 })
    }))
    server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json({ ...ready, pending: [] })))
    renderNew()
    await enterProject()
    await userEvent.type(screen.getByLabelText('Setup command'), 'uv sync --locked')
    await userEvent.click(screen.getByRole('button', { name: 'Start onboarding' }))
    expect(await screen.findByRole('heading', { name: 'Onboarding received' })).toBeInTheDocument()
    expect(submitted).toEqual({ inspect: true, repos: ['jesdi/analytics'], board: { owner: 'jesdi', number: 4 },
      commands: [{ setup_cmd: 'uv sync --locked', gate_cmd: 'uv run pytest -q', verify_cmd: null, services: null }] })
  })
})

it('starts inspection with only a project and repository, leaving values available for detection', async () => {
  let submitted: unknown
  server.use(http.post('/api/onboarding/analytics', async ({ request }) => {
    submitted = await request.json()
    return HttpResponse.json({ status: 'pending', intent: 'inspect-start', request_revision: 'request-one' }, { status: 202 })
  }), http.get('/api/onboarding/analytics', () => HttpResponse.json(ready)))
  renderNew()
  await userEvent.type(screen.getByLabelText('Project name'), 'analytics')
  await userEvent.type(screen.getByLabelText('GitHub repository'), 'jesdi/analytics')
  await userEvent.click(screen.getByRole('button', { name: 'Start onboarding' }))
  await waitFor(() => expect(submitted).toEqual({ inspect: true, repos: ['jesdi/analytics'], board: null,
    commands: [{ setup_cmd: null, gate_cmd: null, verify_cmd: null, services: null }] }))
})

it('keeps repository commands together when another repository is removed and preserves a failed submission', async () => {
  server.use(http.post('/api/onboarding/analytics', () => HttpResponse.json({ detail: 'The board could not be read' }, { status: 422 })))
  renderNew()
  await enterProject()
  await userEvent.click(screen.getByRole('button', { name: 'Add repository' }))
  const second = within(screen.getByRole('group', { name: 'Repository 2' }))
  await userEvent.type(second.getByLabelText('GitHub repository'), 'jesdi/analytics-ui')
  await userEvent.type(second.getByLabelText('Gate command'), 'pnpm test')
  await userEvent.click(screen.getByRole('button', { name: 'Remove repository 1' }))
  expect(screen.getByLabelText('GitHub repository')).toHaveValue('jesdi/analytics-ui')
  expect(screen.getByLabelText('Gate command')).toHaveValue('pnpm test')
  await userEvent.click(screen.getByRole('button', { name: 'Start onboarding' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The board could not be read')
  expect(screen.getByLabelText('Gate command')).toHaveValue('pnpm test')
})

const request = { inspect: true, repos: ['jesdi/analytics'], board: { owner: 'jesdi', number: 4 },
  commands: [{ setup_cmd: 'uv sync', gate_cmd: 'make gate', verify_cmd: '', services: null }] }
const onboarding = { request_revision: 'request-one', edit_mode: 'repositories', project: 'analytics', status: 'queued', request, proposal: null, revision: '', confirmed_revision: '', awaiting: '', notice: '',
  steps: [], pr_url: '', updated_at: '', pending: [{ id: '100-add', action: 'add', actor: 'agent', created_at: '2026-11-03T10:00:00Z' }] }

function renderDetail() {
  return renderWithProviders(<Routes><Route path="/onboarding/projects/:project" element={<OnboardingPage />} /></Routes>, { route: '/onboarding/projects/analytics' })
}

it('finds CLI and queued onboardings in one list and opens a queued request before a record exists', async () => {
  server.use(http.get('/api/onboarding', () => HttpResponse.json({ onboardings: [onboarding] })),
    http.get('/api/onboarding/analytics', () => HttpResponse.json(onboarding)))
  renderWithProviders(<Routes><Route path="/onboarding" element={<OnboardingsPage />} />
    <Route path="/onboarding/projects/:project" element={<OnboardingPage />} /></Routes>, { route: '/onboarding' })
  await userEvent.click(await screen.findByRole('link', { name: /analytics/ }))
  expect(await screen.findByRole('heading', { name: 'analytics' })).toBeInTheDocument()
  expect(screen.getByText('Waiting for the box')).toBeInTheDocument()
  expect(screen.getByText(/Requested by agent/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resume onboarding' })).not.toBeInTheDocument()
})

const proposal = { name: 'analytics', paused: true, board: request.board,
  repositories: [{ name: 'analytics', repo: 'jesdi/analytics', github_id: 202, clone_path: '/home/agent/repos/analytics/analytics',
    worktrees_path: '/home/agent/repos/analytics/analytics.worktrees', ...request.commands[0] }] }
const ready = { ...onboarding, can_confirm: true, status: 'open', pending: [], proposal, revision: 'rev-one', awaiting: 'confirm',
  steps: [{ name: 'resolve', state: 'verified', detail: 'Repository found', link: '', since: '' },
    { name: 'confirm', state: 'blocked', detail: 'Review the proposal', link: '', since: '' }] }

it('reviews the resolved proposal before confirming the exact displayed revision', async () => {
  let confirmed: unknown
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(ready)),
    http.post('/api/onboarding/analytics/confirm', async ({ request: req }) => {
      confirmed = await req.json()
      return HttpResponse.json({ status: 'pending', intent: '200-confirm' }, { status: 202 })
    }))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Review proposal' }))
  const review = screen.getByRole('region', { name: 'Review proposal' })
  expect(within(review).getByText('make gate')).toBeInTheDocument()
  expect(within(review).getByText('jesdi/4')).toBeInTheDocument()
  await userEvent.click(within(review).getByText('Configuration details'))
  expect(within(review).getByText(/\/home\/agent\/repos\/analytics\/analytics/)).toBeInTheDocument()
  await userEvent.click(within(review).getByRole('button', { name: 'Confirm proposal' }))
  await waitFor(() => expect(confirmed).toEqual({ revision: 'rev-one' }))
})

it('keeps the reviewed revision frozen when polling receives a newer proposal', async () => {
  let current = ready
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(current)))
  const { queryClient } = renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Review proposal' }))
  current = { ...ready, revision: 'rev-two', proposal: { ...proposal, repositories: [{ ...proposal.repositories[0]!, gate_cmd: 'make stricter-gate' }] } }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(await screen.findByRole('alert')).toHaveTextContent('The proposal changed')
  expect(screen.queryByRole('button', { name: 'Confirm proposal' })).not.toBeInTheDocument()
  expect(screen.getByText('make gate')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Review latest proposal' }))
  expect(screen.getByText('make stricter-gate')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Confirm proposal' })).toBeEnabled()
})

it('resumes a failed onboarding without replaying the old request and shows the pending action', async () => {
  let current = { ...ready, awaiting: '', steps: [{ name: 'clone', state: 'failed', detail: 'Clone timed out; resume to retry', link: '', since: '' }], pending: [] as typeof onboarding.pending }
  let body = 'not called'
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(current)),
    http.post('/api/onboarding/analytics/resume', async ({ request: req }) => {
      body = await req.text()
      current = { ...current, pending: [{ id: '300-resume', action: 'resume', actor: 'operator', created_at: '' }] }
      return HttpResponse.json({ status: 'pending', intent: '300-resume' }, { status: 202 })
    }))
  renderDetail()
  expect(await screen.findByText('Clone timed out; resume to retry')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Resume onboarding' }))
  expect(await screen.findByText('Waiting for the box')).toBeInTheDocument()
  expect(body).toBe('')
  expect(screen.getByRole('button', { name: 'Resume onboarding' })).toBeDisabled()
})

it('keeps edits pending until the engine accepts the submitted version', async () => {
  let saved: unknown
  let current = { ...ready, confirmed_revision: 'rev-one', awaiting: '', edit_mode: 'details', pending: [] as typeof onboarding.pending }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(current)),
    http.post('/api/onboarding/analytics', async ({ request: req }) => {
      saved = await req.json()
      current = { ...current, pending: [{ id: '400-add', action: 'add', actor: 'operator', created_at: '' }] }
      return HttpResponse.json({ status: 'pending', intent: '400-add', request_revision: 'request-edited' }, { status: 202 })
    }))
  const { queryClient } = renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  expect(screen.getByLabelText('Project name')).toBeDisabled()
  expect(screen.getByLabelText('GitHub repository')).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Add repository' })).not.toBeInTheDocument()
  await userEvent.clear(screen.getByLabelText('Gate command'))
  await userEvent.type(screen.getByLabelText('Gate command'), 'make stricter-gate')
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  await waitFor(() => expect(saved).toEqual({ ...request, expected_request_revision: 'request-one', commands: [{ ...request.commands[0], gate_cmd: 'make stricter-gate' }] }))
  expect(screen.getByLabelText('Gate command')).toHaveValue('make stricter-gate')
  expect(screen.getByLabelText('Gate command')).toBeDisabled()
  expect(await screen.findByText('Waiting for the box to apply your changes.')).toBeInTheDocument()
  current = { ...current, pending: [], request_revision: 'request-edited' }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(await screen.findByRole('heading', { name: 'Onboarding progress' })).toBeInTheDocument()
})

it('removes confirmation when another operator has already confirmed the reviewed proposal', async () => {
  let current = ready
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(current)))
  const { queryClient } = renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Review proposal' }))
  current = { ...ready, awaiting: '', confirmed_revision: 'rev-one' }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm proposal' })).not.toBeInTheDocument())
  expect(screen.getByText('This proposal no longer needs confirmation.')).toBeInTheDocument()
})

it('shows completion and the paused project link without offering edits or resume', async () => {
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json({ ...ready, status: 'complete', edit_mode: 'none', awaiting: '', notice: 'already onboarded',
    pr_url: 'https://github.com/example/infra/pull/12', steps: [{ name: 'config-merge', state: 'verified', detail: 'Merged', link: 'https://github.com/example/infra/pull/12', since: '' }] })))
  renderDetail()
  expect(await screen.findByText(/Onboarding complete/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'View project' })).toHaveAttribute('href', '/?project=analytics')
  expect(screen.getByRole('link', { name: 'Open infrastructure PR' })).toHaveAttribute('href', 'https://github.com/example/infra/pull/12')
  expect(screen.queryByRole('button', { name: 'Edit details' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resume onboarding' })).not.toBeInTheDocument()
})

it('opens a project named new as a detail page rather than the new-project form', async () => {
  server.use(http.post('/api/onboarding/new', () => HttpResponse.json({ status: 'pending', intent: '500-add', request_revision: 'request-one' }, { status: 202 })),
    http.get('/api/onboarding/new', () => HttpResponse.json({ ...ready, project: 'new', pending: [] })))
  renderWithProviders(<Routes>
    <Route path="/onboarding/new" element={<NewOnboardingPage />} />
    <Route path="/onboarding/projects/:project" element={<h1>New project received</h1>} />
  </Routes>, { route: '/onboarding/new' })
  await enterProject()
  await userEvent.clear(screen.getByLabelText('Project name'))
  await userEvent.type(screen.getByLabelText('Project name'), 'new')
  await userEvent.click(screen.getByRole('button', { name: 'Start onboarding' }))
  expect(await screen.findByRole('heading', { name: 'New project received' })).toBeInTheDocument()
})


it('keeps a stale edit draft after a conflict and reloads the latest request only on demand', async () => {
  let current = ready
  let submitted: unknown
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(current)),
    http.post('/api/onboarding/analytics', async ({ request: req }) => {
      submitted = await req.json()
      return HttpResponse.json({ detail: 'request changed; reload the current details before editing' }, { status: 409 })
    }))
  const { queryClient } = renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  await userEvent.clear(screen.getByLabelText('Gate command'))
  await userEvent.type(screen.getByLabelText('Gate command'), 'my gate')
  current = { ...ready, request_revision: 'request-two', request: { ...request, commands: [{ ...request.commands[0]!, gate_cmd: 'their gate' }] } }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(screen.getByLabelText('Gate command')).toHaveValue('my gate')
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('request changed')
  expect(submitted).toEqual({ ...request, expected_request_revision: 'request-one', commands: [{ ...request.commands[0]!, gate_cmd: 'my gate' }] })
  expect(screen.getByLabelText('Gate command')).toHaveValue('my gate')
  await userEvent.click(screen.getByRole('button', { name: 'Reload current details' }))
  expect(screen.getByLabelText('Gate command')).toHaveValue('their gate')
})

it('keeps the draft when a queued edit is refused after another intent wins', async () => {
  let current = { ...ready, pending: [] as typeof onboarding.pending }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(current)),
    http.post('/api/onboarding/analytics', () => {
      current = { ...current, pending: [{ id: 'queued-edit', action: 'add', actor: 'operator', created_at: '' }] }
      return HttpResponse.json({ status: 'pending', intent: 'queued-edit', request_revision: 'my-version' }, { status: 202 })
    }))
  const { queryClient } = renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  await userEvent.clear(screen.getByLabelText('Gate command'))
  await userEvent.type(screen.getByLabelText('Gate command'), 'my gate')
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  expect(await screen.findByText('Waiting for the box to apply your changes.')).toBeInTheDocument()
  current = { ...current, pending: [], request_revision: 'another-version', notice: 'request refused: details changed',
    request: { ...request, commands: [{ ...request.commands[0]!, gate_cmd: 'another gate' }] } }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(await screen.findByRole('alert')).toHaveTextContent('request refused: details changed')
  expect(screen.getByLabelText('Gate command')).toHaveValue('my gate')
  expect(screen.getByRole('button', { name: 'Save details' })).toBeEnabled()
  await userEvent.click(screen.getByRole('button', { name: 'Reload current details' }))
  expect(screen.getByLabelText('Gate command')).toHaveValue('another gate')
})

it('waits for a fresh read after a queued edit when the first refresh fails', async () => {
  let failRead = false
  let current = ready
  server.use(http.get('/api/onboarding/analytics', () => failRead
    ? HttpResponse.json({ detail: 'refresh unavailable' }, { status: 503 }) : HttpResponse.json(current)),
    http.post('/api/onboarding/analytics', () => {
      failRead = true
      return HttpResponse.json({ status: 'pending', intent: 'queued-edit', request_revision: 'my-version' }, { status: 202 })
    }))
  const { queryClient } = renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  await userEvent.type(screen.getByLabelText('Gate command'), ' --strict')
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  expect(await screen.findByText('Waiting for the box to apply your changes.')).toBeInTheDocument()
  expect(screen.getByLabelText('Gate command')).toHaveValue('make gate --strict')
  expect(screen.queryByText(/Your changes were not applied/)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save details' })).toBeDisabled()
  failRead = false
  current = { ...ready, request_revision: 'my-version' }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(await screen.findByRole('heading', { name: 'Onboarding progress' })).toBeInTheDocument()
})

it('retains an open draft when the engine closes editing and still lets the operator leave', async () => {
  let current = ready
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(current)))
  const { queryClient } = renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  await userEvent.type(screen.getByLabelText('Gate command'), ' --strict')
  current = { ...ready, edit_mode: 'none' }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(await screen.findByRole('alert')).toHaveTextContent('no longer accepts edits')
  expect(screen.getByLabelText('Gate command')).toHaveValue('make gate --strict')
  expect(screen.getByRole('button', { name: 'Save details' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Cancel editing' }))
  expect(await screen.findByRole('heading', { name: 'Onboarding progress' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Edit details' })).not.toBeInTheDocument()
})

it.each(['resume', 'confirm'] as const)('keeps accepted %s pending through a failed refresh and prevents duplicates', async action => {
  let failRead = false
  let posts = 0
  let current = { ...ready, pending: [] as typeof onboarding.pending }
  server.use(http.get('/api/onboarding/analytics', () => failRead
    ? HttpResponse.json({ detail: 'Refresh unavailable' }, { status: 503 }) : HttpResponse.json(current)),
    http.post(`/api/onboarding/analytics/${action}`, () => {
      posts += 1
      failRead = true
      return HttpResponse.json({ status: 'pending', intent: 'accepted-action', request_revision: null }, { status: 202 })
    }))
  const { queryClient } = renderDetail()
  if (action === 'confirm') {
    await userEvent.click(await screen.findByRole('button', { name: 'Review proposal' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm proposal' }))
  } else await userEvent.click(await screen.findByRole('button', { name: 'Resume onboarding' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Refresh unavailable')
  expect(screen.getByText('Waiting for the box')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume onboarding' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Review proposal' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Resume onboarding' }))
  expect(posts).toBe(1)
  failRead = false
  current = { ...ready, pending: [{ id: 'accepted-action', action, actor: 'operator', created_at: '' }] }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(screen.getByText('Waiting for the box')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume onboarding' })).toBeDisabled()
  current = { ...ready, pending: [], awaiting: '' }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Resume onboarding' })).toBeEnabled())
  expect(screen.queryByText('Waiting for the box')).not.toBeInTheDocument()
})


it('keeps a new-project draft until the engine accepts it, including when another start wins', async () => {
  let current = { ...onboarding, pending: [{ id: 'my-start', action: 'add', actor: 'operator', created_at: '' }] }
  server.use(http.post('/api/onboarding/analytics', () => HttpResponse.json({ status: 'pending', intent: 'my-start', request_revision: 'my-version' }, { status: 202 })),
    http.get('/api/onboarding/analytics', () => HttpResponse.json(current)))
  const { queryClient } = renderNew()
  await enterProject()
  await userEvent.click(screen.getByRole('button', { name: 'Start onboarding' }))
  expect(await screen.findByText('Waiting for the box')).toBeInTheDocument()
  expect(screen.getByLabelText('Gate command')).toHaveValue('uv run pytest -q')
  expect(screen.getByLabelText('Gate command')).toBeDisabled()
  current = { ...current, status: 'open', request_revision: 'other-version', pending: [] }
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(await screen.findByRole('alert')).toHaveTextContent('Your draft is kept')
  expect(screen.getByLabelText('Gate command')).toHaveValue('uv run pytest -q')
  expect(screen.getByLabelText('Project name')).toBeEnabled()
  expect(screen.getByRole('link', { name: 'Open existing onboarding' })).toHaveAttribute('href', '/onboarding/projects/analytics')
})

it('retains a submitted new-project draft through a failed first read until the engine accepts it', async () => {
  let failRead = true
  server.use(http.post('/api/onboarding/analytics', () => HttpResponse.json({ status: 'pending', intent: 'my-start', request_revision: 'request-one' }, { status: 202 })),
    http.get('/api/onboarding/analytics', () => failRead
      ? HttpResponse.json({ detail: 'Refresh unavailable' }, { status: 503 }) : HttpResponse.json(ready)))
  const { queryClient } = renderNew()
  await enterProject()
  await userEvent.click(screen.getByRole('button', { name: 'Start onboarding' }))
  expect(await screen.findByText(/Could not refresh onboarding/)).toHaveTextContent('Refresh unavailable')
  expect(screen.getByLabelText('Gate command')).toHaveValue('uv run pytest -q')
  expect(screen.getByLabelText('Gate command')).toBeDisabled()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  failRead = false
  await queryClient.invalidateQueries({ queryKey: ['onboarding'] })
  expect(await screen.findByRole('heading', { name: 'Onboarding received' })).toBeInTheDocument()
})

const inspected = { ...ready, can_confirm: true, inspection: { repositories: [{
  repo: 'jesdi/analytics', state: 'complete', detail: '', request_id: 'inspect-one',
  fields: { setup_cmd: { value: '', source: '' }, gate_cmd: { value: 'make gate', source: 'draft: Makefile target gate' }, verify_cmd: { value: '', source: '' } },
  board: { value: request.board, source: 'rule: .backlog/project-meta.json' },
  services: [{ value: 'postgres', source: 'rule: compose.yaml' }],
  selected_services: [{ value: 'postgres', source: 'draft: integration tests need postgres' }],
  secrets: [{ value: 'DATABASE_URL', source: 'rule: .env.example' }], skills: [],
}] }, request: { repos: ['jesdi/analytics'], board: null, commands: [{ setup_cmd: null, gate_cmd: null, verify_cmd: null, services: null }] },
  proposal: { ...proposal, repositories: [{ ...proposal.repositories[0]!, setup_cmd: '', services: ['postgres'] }] } }

it('edits detected values without turning untouched suggestions into operator overrides', async () => {
  let saved: unknown
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(inspected)),
    http.post('/api/onboarding/analytics', async ({ request: req }) => {
      saved = await req.json()
      return HttpResponse.json({ status: 'pending', intent: 'edit-inspected', request_revision: 'edited' }, { status: 202 })
    }))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  expect(screen.getByLabelText('GitHub board')).toHaveValue('jesdi/4')
  expect(screen.getByLabelText('Gate command')).toHaveValue('make gate')
  expect(screen.getByText('draft: Makefile target gate')).toBeInTheDocument()
  expect(screen.getAllByText('Nothing found')).toHaveLength(2)
  expect(screen.getByText('DATABASE_URL')).toBeInTheDocument()
  expect(screen.getByText('draft: integration tests need postgres')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('checkbox', { name: 'postgres' }))
  await userEvent.type(screen.getByLabelText('Setup command'), 'uv sync')
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  await waitFor(() => expect(saved).toEqual({ inspect: true, expected_request_revision: 'request-one', repos: ['jesdi/analytics'], board: null,
    commands: [{ setup_cmd: 'uv sync', gate_cmd: null, verify_cmd: null, services: [] }] }))
})

it('lets an operator skip a waiting draft and keeps the action pending until the box consumes it', async () => {
  let skipped: unknown
  const waiting = { ...inspected, awaiting: '', can_confirm: false, inspection: { repositories: [{ ...inspected.inspection.repositories[0]!, state: 'waiting' }] },
    steps: [{ name: 'inspect', state: 'waiting', detail: 'waiting for a free execution unit (box full: 2/2)', link: '', since: '' }] }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(waiting)),
    http.post('/api/onboarding/analytics/skip-inspection', async ({ request }) => {
      skipped = await request.json()
      return HttpResponse.json({ status: 'pending', intent: 'skip-one' }, { status: 202 })
    }))
  renderDetail()
  expect(await screen.findByText('waiting for a free execution unit (box full: 2/2)')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Fill in by hand' }))
  await waitFor(() => expect(skipped).toEqual({ revision: 'rev-one' }))
})

it('shows provenance in review and disables confirmation until required fields are filled', async () => {
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json({ ...inspected, can_confirm: false,
    proposal: { ...inspected.proposal, board: null, repositories: [{ ...inspected.proposal.repositories[0]!, gate_cmd: '' }] } })))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Review proposal' }))
  const review = within(screen.getByRole('region', { name: 'Review proposal' }))
  expect(review.getByRole('button', { name: 'Confirm proposal' })).toBeDisabled()
  expect(review.getByText('Choose a board and fill in a gate command for every repository before confirming.')).toBeInTheDocument()
  expect(review.getByText('DATABASE_URL')).toBeInTheDocument()
  expect(review.getByText('rule: .env.example')).toBeInTheDocument()
})

it('matches detected commands to a repository entered as a GitHub URL and preserves an explicit cleared value', async () => {
  let saved: unknown
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json({ ...inspected,
    request: { ...inspected.request, repos: ['https://github.com/Jesdi/Analytics.git'] } })),
    http.post('/api/onboarding/analytics', async ({ request }) => {
      saved = await request.json()
      return HttpResponse.json({ status: 'pending', intent: 'clear-one' }, { status: 202 })
    }))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  expect(screen.getByLabelText('Gate command')).toHaveValue('make gate')
  await userEvent.clear(screen.getByLabelText('Gate command'))
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  await waitFor(() => expect(saved).toMatchObject({ commands: [{ gate_cmd: '', setup_cmd: null, verify_cmd: null }] }))
})

it('distinguishes an intentionally skipped draft from a completed search that found nothing', async () => {
  const skipped = { ...inspected, inspection: { repositories: [{ ...inspected.inspection.repositories[0]!, state: 'skipped',
    fields: { setup_cmd: { value: '', source: '' }, gate_cmd: { value: '', source: '' }, verify_cmd: { value: '', source: '' } } }] } }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(skipped)))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  expect(screen.getAllByText('Draft skipped')).toHaveLength(3)
  expect(screen.queryByText('Not inspected yet')).not.toBeInTheDocument()
})

it('shows that inspection found no services or secret names in proposal review', async () => {
  const emptyFacts = { ...inspected, inspection: { repositories: [{ ...inspected.inspection.repositories[0]!, services: [], secrets: [] }] } }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(emptyFacts)))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Review proposal' }))
  const review = within(screen.getByRole('region', { name: 'Review proposal' }))
  expect(review.getAllByText('Nothing found')).toHaveLength(4)
  expect(review.getByText('Compose services')).toBeInTheDocument()
  expect(review.getByText('Secret names')).toBeInTheDocument()
})

it('selects a detected service by hand and saves the explicit choice', async () => {
  let saved: unknown
  const unselected = { ...inspected, proposal: { ...inspected.proposal,
    repositories: [{ ...inspected.proposal.repositories[0]!, services: [] }] } }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(unselected)),
    http.post('/api/onboarding/analytics', async ({ request }) => {
      saved = await request.json()
      return HttpResponse.json({ status: 'pending', intent: 'selected-service' }, { status: 202 })
    }))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  const service = screen.getByRole('checkbox', { name: 'postgres' })
  expect(service).not.toBeChecked()
  await userEvent.click(service)
  expect(service).toBeChecked()
  expect(screen.getByText('you')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  await waitFor(() => expect(saved).toMatchObject({ commands: [{ services: ['postgres'] }] }))
})

it('distinguishes detected services from selected services in review', async () => {
  const unselected = { ...inspected, proposal: { ...inspected.proposal,
    repositories: [{ ...inspected.proposal.repositories[0]!, services: [] }] } }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(unselected)))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Review proposal' }))
  const review = within(screen.getByRole('region', { name: 'Review proposal' }))
  expect(review.getByText('postgres — not selected')).toBeInTheDocument()
  expect(review.getByText('rule: compose.yaml')).toBeInTheDocument()
})

it('uses detected provenance when a selected service has no draft reason', async () => {
  const ruleSelected = { ...inspected, inspection: { repositories: [{ ...inspected.inspection.repositories[0]!, selected_services: [] }] } }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(ruleSelected)))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Review proposal' }))
  const review = within(screen.getByRole('region', { name: 'Review proposal' }))
  expect(review.getByText('postgres — selected')).toBeInTheDocument()
  expect(review.getByText('rule: compose.yaml')).toBeInTheDocument()
})

it('keeps renamed repository suggestions attached after another row is removed and preserves manual overrides', async () => {
  let saved: unknown
  const renamed = { ...inspected, request: { ...inspected.request, repos: ['jesdi/unused', 'jesdi/old-analytics'],
    commands: [{ setup_cmd: null, gate_cmd: null, verify_cmd: null, services: null }, inspected.request.commands[0]] },
    proposal: { ...inspected.proposal, repositories: [{ ...inspected.proposal.repositories[0]!, repo: 'jesdi/unused', github_id: 999, gate_cmd: 'make other' }, inspected.proposal.repositories[0]!] } }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(renamed)),
    http.post('/api/onboarding/analytics', async ({ request }) => {
      saved = await request.json()
      return HttpResponse.json({ status: 'pending', intent: 'alias-edited' }, { status: 202 })
    }))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  await userEvent.click(screen.getByRole('button', { name: 'Remove repository 1' }))
  expect(screen.getByLabelText('GitHub repository')).toHaveValue('jesdi/old-analytics')
  expect(screen.getByLabelText('Gate command')).toHaveValue('make gate')
  expect(screen.getByText('draft: Makefile target gate')).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'postgres' })).toBeChecked()
  await userEvent.clear(screen.getByLabelText('Gate command'))
  await userEvent.type(screen.getByLabelText('Gate command'), 'make my-gate')
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  await waitFor(() => expect(saved).toMatchObject({ repos: ['jesdi/old-analytics'],
    commands: [{ setup_cmd: null, gate_cmd: 'make my-gate', verify_cmd: null, services: null }] }))
})

it('does not borrow a renamed repository suggestion when its row is changed to a different repository', async () => {
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json({ ...inspected,
    request: { ...inspected.request, repos: ['jesdi/old-analytics'] } })))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  expect(screen.getByLabelText('Gate command')).toHaveValue('make gate')
  await userEvent.clear(screen.getByLabelText('GitHub repository'))
  await userEvent.type(screen.getByLabelText('GitHub repository'), 'jesdi/unrelated')
  expect(screen.getByLabelText('Gate command')).toHaveValue('')
  expect(screen.queryByText('draft: Makefile target gate')).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: 'postgres' })).not.toBeInTheDocument()
})

it('does not reuse a removed row association for a newly added repository', async () => {
  const two = { ...inspected, request: { ...inspected.request, repos: ['jesdi/unused', 'jesdi/old-analytics'],
    commands: [{ setup_cmd: null, gate_cmd: null, verify_cmd: null, services: null }, inspected.request.commands[0]] },
    proposal: { ...inspected.proposal, repositories: [{ ...inspected.proposal.repositories[0]!, repo: 'jesdi/unused', github_id: 999, gate_cmd: 'make other' }, inspected.proposal.repositories[0]!] } }
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json(two)))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  await userEvent.click(screen.getByRole('button', { name: 'Remove repository 2' }))
  await userEvent.click(screen.getByRole('button', { name: 'Add repository' }))
  const added = within(screen.getByRole('group', { name: 'Repository 2' }))
  await userEvent.type(added.getByLabelText('GitHub repository'), 'jesdi/old-analytics')
  expect(added.getByLabelText('Gate command')).toHaveValue('')
  expect(added.queryByRole('checkbox', { name: 'postgres' })).not.toBeInTheDocument()
})

it('does not associate a new request with a stale proposal after repository resolution fails', async () => {
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json({ ...inspected,
    request: { ...inspected.request, repos: ['jesdi/missing'] },
    steps: [{ name: 'resolve', state: 'blocked', detail: 'Repository could not be found', link: '', since: '' }] })))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  expect(screen.getByLabelText('Gate command')).toHaveValue('')
  expect(screen.queryByText('draft: Makefile target gate')).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: 'postgres' })).not.toBeInTheDocument()
})
