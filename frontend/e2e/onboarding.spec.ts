import { expect, test } from '@playwright/test'

for (const project of ['analytics', 'new']) {
test(`operator starts project ${project} from the worksheet and sees its queued onboarding`, async ({ page }) => {
  let submitted: unknown
  const request = {
    repos: ['jesdi/analytics'], board: { owner: 'jesdi', number: 4 },
    commands: [{ setup_cmd: 'uv sync --locked', gate_cmd: 'uv run pytest -q', verify_cmd: '' }],
  }
  await page.route(`**/api/onboarding/${project}`, async route => {
    if (route.request().method() === 'POST') {
      submitted = route.request().postDataJSON()
      await route.fulfill({ status: 202, json: { status: 'pending', intent: 'start-analytics' } })
    } else {
      await route.fulfill({ json: {
        project, status: 'queued', request, proposal: null,
        revision: '', confirmed_revision: '', awaiting: '', notice: '', steps: [],
        pr_url: '', updated_at: '',
        pending: [{ action: 'add', actor: 'jesdi', created_at: '', id: 'start-analytics' }],
      } })
    }
  })
  await page.goto('/onboarding/new')
  await page.getByLabel('Project name', { exact: true }).fill(project)
  await page.getByLabel('GitHub board', { exact: true }).fill('jesdi/4')
  await page.getByLabel('GitHub repository', { exact: true }).fill('jesdi/analytics')
  await page.getByLabel('Setup command', { exact: true }).fill('uv sync --locked')
  await page.getByLabel('Gate command', { exact: true }).fill('uv run pytest -q')
  await page.getByRole('button', { name: 'Start onboarding', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/onboarding/projects/${project}$`))
  expect(submitted).toEqual(request)
  await expect(page.getByText(/queued|waiting for the box/i).first()).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(page.viewportSize()!.width)
})

}

test('operator reviews a CLI proposal, confirms it and resumes after an infrastructure failure', async ({ page }) => {
  let phase: 'review' | 'failed' | 'waiting' = 'review'
  let confirmed: unknown
  let resumed = false
  const proposal = { name: 'analytics', paused: true, board: { owner: 'jesdi', number: 4 },
    repositories: [{ name: 'analytics', repo: 'jesdi/analytics', github_id: 202,
      clone_path: '/home/agent/repos/analytics/analytics', worktrees_path: '/home/agent/repos/analytics/analytics.worktrees',
      setup_cmd: 'uv sync --locked', gate_cmd: 'uv run pytest -q', verify_cmd: '' }] }
  await page.route('**/api/onboarding/analytics', route => route.fulfill({ json: {
    project: 'analytics', status: 'open',
    request: { repos: ['jesdi/analytics'], board: proposal.board,
      commands: [{ setup_cmd: 'uv sync --locked', gate_cmd: 'uv run pytest -q', verify_cmd: '' }] },
    proposal, revision: 'revision-reviewed', confirmed_revision: phase === 'review' ? '' : 'revision-reviewed',
    awaiting: phase === 'review' ? 'confirm' : '', notice: '', pending: [],
    updated_at: '2026-11-03T10:00:00Z', pr_url: 'https://github.com/example/infra/pull/12',
    steps: [{ name: 'resolve', state: 'verified', detail: 'Repositories resolved', link: '', since: '' },
      { name: 'clone', state: 'verified', detail: 'Repositories cloned', link: '', since: '' },
      { name: phase === 'review' ? 'confirm' : 'config-merge',
        state: phase === 'review' ? 'blocked' : phase === 'failed' ? 'failed' : 'waiting',
        detail: phase === 'review' ? 'Review the proposal' : phase === 'failed' ? 'Infrastructure checks failed; resolve the failed check and resume.' : 'Waiting for infrastructure checks',
        link: '', since: '' }],
  } }))
  await page.route('**/api/onboarding/analytics/confirm', async route => {
    confirmed = route.request().postDataJSON()
    phase = 'failed'
    await route.fulfill({ status: 202, json: { status: 'pending', intent: 'confirm-analytics' } })
  })
  await page.route('**/api/onboarding/analytics/resume', async route => {
    expect(route.request().postData()).toBeNull()
    resumed = true
    phase = 'waiting'
    await route.fulfill({ status: 202, json: { status: 'pending', intent: 'resume-analytics' } })
  })
  await page.goto('/onboarding/projects/analytics')
  await page.getByRole('button', { name: 'Review proposal', exact: true }).click()
  const review = page.getByRole('region', { name: 'Review proposal', exact: true })
  await expect(review.getByText('uv run pytest -q', { exact: true })).toBeVisible()
  await review.getByRole('button', { name: 'Confirm proposal', exact: true }).click()
  expect(confirmed).toEqual({ revision: 'revision-reviewed' })
  await expect(page.getByText('Infrastructure checks failed; resolve the failed check and resume.', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Open infrastructure PR' })).toHaveAttribute('href', 'https://github.com/example/infra/pull/12')
  await page.getByRole('button', { name: 'Resume onboarding', exact: true }).click()
  await expect(page.getByText('Waiting for infrastructure checks', { exact: true })).toBeVisible()
  expect(resumed).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(page.viewportSize()!.width)
})
