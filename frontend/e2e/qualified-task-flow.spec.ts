import { expect, test } from '@playwright/test'

const FRONT = 'example/shop-frontend'
const BACK = 'example/shop-backend'
const rid = (repo: string) => repo.replace('/', '~')

test('colliding cards keep page content, messages and actions separate within one cache lifetime', async ({ page, request }) => {
  await request.post('/__control__/reset-messages')
  const seed = await (await request.get('/api/task/widget/42')).json()
  const board = await (await request.get('/api/board')).json()
  const column = board.columns.find((c: { cards: object[] }) => c.cards.length)
  const cards = [FRONT, BACK].map(repo => ({ ...seed.card, target: 'widget', repo, issue: 12, title: `title of ${repo}` }))
  await page.route('**/api/board', route => route.fulfill({
    json: { ...board, columns: board.columns.map((c: { key: string }) =>
      ({ ...c, cards: c.key === column.key ? cards : [] })) },
  }))
  const posts: { path: string; text: string }[] = []
  for (const repo of [FRONT, BACK]) {
    await page.route(`**/api/task/widget/${rid(repo)}/12`, route => route.fulfill({
      json: { ...seed, repo, title: `title of ${repo}`,
        card: cards.find(c => c.repo === repo),
        messages: [{ id: repo, text: `message for ${repo}`, actor: 'operator', state: 'queued', created_at: '', delivered_at: '' }] },
    }))
    await page.route(`**/api/task/widget/${rid(repo)}/12/*`, route => {
      if (route.request().method() === 'POST') {
        posts.push({ path: new URL(route.request().url()).pathname, text: route.request().postDataJSON().text })
        return route.fulfill({ status: 202, json: { status: 'pending', intent: 'i' } })
      }
      return route.fulfill({ json: null })
    })
  }

  // One document and QueryClient: return through the shell's router link,
  // visit the sibling, then revisit the first task with both caches populated.
  await page.goto('/')
  let count = 0
  for (const repo of [FRONT, BACK, FRONT]) {
    const other = repo === FRONT ? BACK : FRONT
    await expect(page.getByRole('link', { name: /title of example/ })).toHaveCount(2)
    await page.getByRole('link', { name: `title of ${repo}`, exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`/${rid(repo)}/12$`))
    await expect(page.getByRole('heading', { name: `title of ${repo}` })).toBeVisible()
    await expect(page.getByTestId('message-thread')).toContainText(`message for ${repo}`)
    await expect(page.getByTestId('message-thread')).not.toContainText(`message for ${other}`)
    await page.getByLabel('Reply').fill(`for ${repo}`)
    await page.getByRole('button', { name: 'Send reply & wake' }).click()
    await expect.poll(() => posts.length).toBe(++count)
    await page.getByRole('link', { name: 'Board', exact: true }).click()
  }
  expect(posts).toEqual([FRONT, BACK, FRONT].map(repo => ({
    path: `/api/task/widget/${rid(repo)}/12/reply`, text: `for ${repo}`,
  })))
})
