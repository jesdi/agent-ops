import { expect, test } from '@playwright/test'

test('inspection fills the worksheet and preserves an operator override through proposal review', async ({ page }) => {
  let started = false
  let inspected = false
  let edited = false
  let confirmed: unknown
  const commands = { setup_cmd: null, gate_cmd: null as string | null, verify_cmd: null, services: null }
  const request = { repos: ['example/analytics'], board: null, commands: [commands], inspect: true }
  const repository = { name: 'analytics', repo: 'example/analytics', github_id: 202,
    clone_path: '/home/agent/repos/analytics/analytics', worktrees_path: '/home/agent/repos/analytics/analytics.worktrees',
    setup_cmd: '', gate_cmd: 'make gate', verify_cmd: '', services: ['postgres'] }
  const report = { repo: repository.repo, state: 'complete', detail: '', request_id: 'draft-one',
    fields: { setup_cmd: { value: '', source: '' }, gate_cmd: { value: 'make gate', source: 'draft: Makefile target gate' }, verify_cmd: { value: '', source: '' } },
    board: { value: { owner: 'example', number: 4 }, source: 'rule: .backlog/project-meta.json' },
    services: [{ value: 'postgres', source: 'rule: compose.yaml' }],
    secrets: [{ value: 'DATABASE_URL', source: 'rule: .env.example' }], skills: [] }
  await page.route('**/api/onboarding/analytics', async route => {
    if (route.request().method() === 'POST') {
      const payload = route.request().postDataJSON()
      if (started) {
        expect(payload.expected_request_revision).toBe('request-start')
        expect(payload.commands[0].gate_cmd).toBe('make gate-ci')
        expect(payload.commands[0].setup_cmd).toBeNull()
        edited = true
        commands.gate_cmd = 'make gate-ci'
      } else {
        expect(payload.board).toBeNull()
        expect(payload.commands[0].gate_cmd).toBeNull()
        started = true
      }
      await route.fulfill({ status: 202, json: { status: 'pending', intent: edited ? 'edit-one' : 'start-one', request_revision: edited ? 'request-edited' : 'request-start' } })
    } else await route.fulfill({ json: {
      project: 'analytics', status: 'open', request, request_revision: edited ? 'request-edited' : 'request-start', edit_mode: 'repositories',
      proposal: { name: 'analytics', paused: true, board: inspected ? report.board.value : null,
        repositories: [{ ...repository, gate_cmd: edited ? 'make gate-ci' : inspected ? 'make gate' : '' }] },
      inspection: { repositories: [{ ...report, state: inspected ? 'complete' : 'waiting',
        fields: { ...report.fields, gate_cmd: edited ? { value: 'make gate-ci', source: 'you' } : report.fields.gate_cmd } }] },
      can_confirm: inspected, revision: edited ? 'proposal-edited' : 'proposal-start', confirmed_revision: '',
      awaiting: inspected ? 'confirm' : '', notice: '', pending: [], pr_url: '', updated_at: '',
      steps: [{ name: 'inspect', state: inspected ? 'verified' : 'waiting',
        detail: inspected ? 'Repository inspected' : 'waiting for a free execution unit (box full: 2/2)', link: '', since: '' }],
    } })
  })
  await page.route('**/api/onboarding/analytics/confirm', async route => {
    confirmed = route.request().postDataJSON()
    await route.fulfill({ status: 202, json: { status: 'pending', intent: 'confirm-one' } })
  })
  await page.goto('/onboarding/new')
  await page.getByLabel('Project name', { exact: true }).fill('analytics')
  await page.getByLabel('GitHub repository', { exact: true }).fill('example/analytics')
  await page.getByRole('button', { name: 'Start onboarding', exact: true }).click()
  await expect(page).toHaveURL(/\/onboarding\/projects\/analytics$/)
  await expect(page.getByText('waiting for a free execution unit (box full: 2/2)')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Fill in by hand' })).toBeVisible()
  inspected = true
  await expect(page.getByText('Repository inspected')).toBeVisible()
  await page.getByRole('button', { name: 'Edit details' }).click()
  await expect(page.getByLabel('GitHub board', { exact: true })).toHaveValue('example/4')
  await expect(page.getByLabel('Gate command', { exact: true })).toHaveValue('make gate')
  await expect(page.getByText('draft: Makefile target gate')).toBeVisible()
  await expect(page.getByRole('checkbox', { name: 'postgres' })).toBeChecked()
  await expect(page.getByText('DATABASE_URL', { exact: true })).toBeVisible()
  await page.getByLabel('Gate command', { exact: true }).fill('make gate-ci')
  await page.getByRole('button', { name: 'Save details' }).click()
  await page.getByRole('button', { name: 'Review proposal', exact: true }).click()
  const review = page.getByRole('region', { name: 'Review proposal', exact: true })
  await expect(review.getByText('make gate-ci', { exact: true })).toBeVisible()
  await expect(review.getByText('you', { exact: true })).toBeVisible()
  await review.getByRole('button', { name: 'Confirm proposal', exact: true }).click()
  expect(confirmed).toEqual({ revision: 'proposal-edited' })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(page.viewportSize()!.width)
})
