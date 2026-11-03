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
      return HttpResponse.json({ status: 'pending', intent: '100-add' }, { status: 202 })
    }))
    renderNew()
    await enterProject()
    await userEvent.type(screen.getByLabelText('Setup command'), 'uv sync --locked')
    await userEvent.click(screen.getByRole('button', { name: 'Start onboarding' }))
    expect(await screen.findByRole('heading', { name: 'Onboarding received' })).toBeInTheDocument()
    expect(submitted).toEqual({ repos: ['jesdi/analytics'], board: { owner: 'jesdi', number: 4 },
      commands: [{ setup_cmd: 'uv sync --locked', gate_cmd: 'uv run pytest -q', verify_cmd: '' }] })
  })
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

const request = { repos: ['jesdi/analytics'], board: { owner: 'jesdi', number: 4 },
  commands: [{ setup_cmd: 'uv sync', gate_cmd: 'make gate', verify_cmd: '' }] }
const onboarding = { project: 'analytics', status: 'queued', request, proposal: null, revision: '', confirmed_revision: '', awaiting: '', notice: '',
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
const ready = { ...onboarding, status: 'open', pending: [], proposal, revision: 'rev-one', awaiting: 'confirm',
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

it('edits commands after confirmation while keeping project and repository identities fixed', async () => {
  let saved: unknown
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json({ ...ready, confirmed_revision: 'rev-one', awaiting: '' })),
    http.post('/api/onboarding/analytics', async ({ request: req }) => {
      saved = await req.json()
      return HttpResponse.json({ status: 'pending', intent: '400-add' }, { status: 202 })
    }))
  renderDetail()
  await userEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
  expect(screen.getByLabelText('Project name')).toBeDisabled()
  expect(screen.getByLabelText('GitHub repository')).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Add repository' })).not.toBeInTheDocument()
  await userEvent.clear(screen.getByLabelText('Gate command'))
  await userEvent.type(screen.getByLabelText('Gate command'), 'make stricter-gate')
  await userEvent.click(screen.getByRole('button', { name: 'Save details' }))
  await waitFor(() => expect(saved).toEqual({ ...request, commands: [{ ...request.commands[0], gate_cmd: 'make stricter-gate' }] }))
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
  server.use(http.get('/api/onboarding/analytics', () => HttpResponse.json({ ...ready, status: 'complete', awaiting: '', notice: 'already onboarded',
    pr_url: 'https://github.com/example/infra/pull/12', steps: [{ name: 'config-merge', state: 'verified', detail: 'Merged', link: 'https://github.com/example/infra/pull/12', since: '' }] })))
  renderDetail()
  expect(await screen.findByText(/Onboarding complete/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'View project' })).toHaveAttribute('href', '/?project=analytics')
  expect(screen.getByRole('link', { name: 'Open infrastructure PR' })).toHaveAttribute('href', 'https://github.com/example/infra/pull/12')
  expect(screen.queryByRole('button', { name: 'Edit details' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resume onboarding' })).not.toBeInTheDocument()
})

it('opens a project named new as a detail page rather than the new-project form', async () => {
  server.use(http.post('/api/onboarding/new', () => HttpResponse.json({ status: 'pending', intent: '500-add' }, { status: 202 })))
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
