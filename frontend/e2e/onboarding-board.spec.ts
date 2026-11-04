import { expect, test } from '@playwright/test'

const proposal = { name: 'analytics', paused: true, board: { owner: 'jesdi', new: true, title: 'analytics Backlog' },
  repositories: [{ name: 'analytics', repo: 'jesdi/analytics', github_id: 202,
    clone_path: '/home/agent/repos/analytics/analytics', worktrees_path: '/home/agent/repos/analytics/analytics.worktrees',
    setup_cmd: '', gate_cmd: 'make gate', verify_cmd: '' }] }
const boardReview = ["create board 'analytics Backlog' for jesdi",
  'add labels on jesdi/analytics: auto, human-required, inbox, spec-ready, track:standard']

test('operator asks for a new board and reviews what the board step will add', async ({ page }) => {
  let submitted: unknown
  let processed = false
  await page.route('**/api/onboarding/analytics', async route => {
    if (route.request().method() === 'POST') {
      submitted = route.request().postDataJSON()
      processed = true
      await route.fulfill({ status: 202, json: { status: 'pending', intent: 'start-analytics', request_revision: 'request-new' } })
      return
    }
    await route.fulfill({ json: {
      project: 'analytics', status: processed ? 'open' : 'queued', can_confirm: true,
      request: { inspect: true, repos: ['jesdi/analytics'], board: { new: true }, commands: [{ gate_cmd: 'make gate' }] },
      request_revision: 'request-new', edit_mode: 'repositories', proposal, revision: 'revision-new',
      confirmed_revision: '', awaiting: 'confirm', notice: '', board_review: boardReview, pending: [],
      updated_at: '', pr_url: '',
      steps: [{ name: 'resolve', state: 'verified', detail: '', link: '', since: '' },
        { name: 'confirm', state: 'blocked', detail: 'Review the proposal', link: '', since: '' }],
    } })
  })
  await page.goto('/onboarding/new')
  await page.getByLabel('Project name', { exact: true }).fill('analytics')
  await page.getByLabel('Create a new board', { exact: true }).check()
  await expect(page.getByLabel('GitHub board', { exact: true })).toBeDisabled()
  await expect(page.getByRole('complementary', { name: 'Project at a glance' })
    .getByText('New board: analytics Backlog', { exact: true })).toBeVisible()
  await page.getByLabel('GitHub repository', { exact: true }).fill('jesdi/analytics')
  await page.getByLabel('Gate command', { exact: true }).fill('make gate')
  await page.screenshot({ path: test.info().outputPath('new-board-form.png'), fullPage: true })
  await page.getByRole('button', { name: 'Start onboarding', exact: true }).click()
  await expect.poll(() => submitted).toMatchObject({ board: { new: true } })

  await expect(page).toHaveURL(/\/onboarding\/projects\/analytics$/, { timeout: 10000 })
  await page.getByRole('button', { name: 'Review proposal', exact: true }).click()
  const review = page.getByRole('region', { name: 'Review proposal', exact: true })
  await expect(review.getByText('New board: analytics Backlog', { exact: true })).toBeVisible()
  await expect(review.getByRole('list', { name: 'Board changes' }).getByRole('listitem')).toHaveText(boardReview)
  await page.screenshot({ path: test.info().outputPath('new-board-review.png'), fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(page.viewportSize()!.width)
})
