import { expect, test } from '@playwright/test'
import { showColumn } from './phone.js'

const project = (name: string, extra = {}) =>
  ({ name, state: 'enabled', repositories: [`example/${name}`], pending: null, ...extra })

test('project tabs scope queue actions, show pending changes and report a refused control', async ({ page, request }) => {
  const board = await (await request.get('/api/board')).json()
  const ghost = (target: string, number: number) =>
    ({ number, target, repo: `example/${target}`, title: `${target} queued`, url: 'u', score: 5, boost: 0 })
  const seed = await (await request.get('/api/task/widget/42')).json()
  const taskColumn = board.columns.find((c: { cards: object[] }) => c.cards.length).key
  const cards = ['alpha', 'beta'].map(target =>
    ({ ...seed.card, target, repo: `example/${target}`, issue: 30, title: `${target} task` }))
  const replies: { path: string; text: string }[] = []
  for (const target of ['alpha', 'beta']) {
    await page.route(`**/api/task/${target}/example~${target}/30`, route => route.fulfill({
      json: { ...seed, target, issue: 30, repo: `example/${target}`, title: `${target} task`, card: cards.find(c => c.target === target), messages: [], dependency_errors: [], required_prs: [] },
    }))
    await page.route(`**/api/task/${target}/example~${target}/30/*`, route => {
      if (route.request().method() !== 'POST') return route.fulfill({ json: null })
      replies.push({ path: new URL(route.request().url()).pathname, text: route.request().postDataJSON().text })
      return route.fulfill({ status: 202, json: { status: 'pending', intent: 'i' } })
    })
  }
  await page.route('**/api/board', route => route.fulfill({
    json: { ...board, columns: board.columns.map((c: { key: string }) =>
      ({ ...c, cards: c.key === taskColumn ? cards : [], ghosts: c.key === 'queued' ? [ghost('alpha', 11), ghost('beta', 12)] : [] })) },
  }))
  await page.route('**/api/projects', route => route.fulfill({ json: { projects: [
    project('alpha'),
    project('beta', { state: 'paused', pending: { operation: 'ctl-1', requested: 'enabled', phase: 'blocked',
      blocker: 'check unit-tests failing', pr: { number: 7, url: 'https://x/7' } } }),
  ] } }))
  const posts: { path: string; body: unknown }[] = []
  await page.route('**/api/queue/*', route => {
    posts.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON() })
    return route.fulfill({ json: {} })
  })
  await page.route('**/api/projects/alpha/pause', route =>
    route.fulfill({ status: 503, json: { detail: 'control service unavailable' } }))

  await page.goto('/')
  await page.getByRole('tab', { name: 'beta' }).click()
  await expect(page.getByTestId('project-pending')).toContainText('Pending: enabled (blocked) — check unit-tests failing')
  await showColumn(page, taskColumn)
  await expect(page.getByRole('link', { name: 'alpha task', exact: true })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'beta task', exact: true })).toHaveCount(1)
  await page.getByRole('link', { name: 'beta task', exact: true }).click()
  await page.getByLabel('Reply').fill('for beta')
  await page.getByRole('button', { name: 'Send reply & wake' }).click()
  await expect.poll(() => replies).toEqual([{ path: '/api/task/beta/example~beta/30/reply', text: 'for beta' }])
  await page.getByRole('link', { name: 'Board', exact: true }).click()
  await page.getByRole('tab', { name: 'beta' }).click()
  await showColumn(page, 'queued')
  const queued = page.getByTestId('column-queued')
  await expect(queued.getByText('alpha queued')).toHaveCount(0)
  await queued.getByRole('button', { name: 'Details for beta#12' }).click()
  await queued.getByRole('button', { name: 'Next' }).click()
  await expect.poll(() => posts).toEqual([
    { path: '/api/queue/next', body: expect.objectContaining({ issue: 12, repo: 'example/beta' }) },
  ])

  await page.getByRole('tab', { name: 'alpha' }).click()
  await expect(queued.getByText('beta queued')).toHaveCount(0)
  await showColumn(page, taskColumn)
  await expect(page.getByRole('link', { name: 'beta task', exact: true })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'alpha task', exact: true })).toHaveCount(1)
  await showColumn(page, 'queued')
  await page.getByRole('button', { name: 'pause', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm pause' }).click()
  await expect(page.getByRole('alert')).toContainText('control service unavailable')
  await expect(page.getByText('enabled', { exact: true })).toBeVisible()
})
