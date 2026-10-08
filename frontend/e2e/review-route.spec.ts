import { readFileSync } from 'node:fs'
import { expect, test, type Page } from '@playwright/test'

// Ticket 06: the review route and the bridge, against the released template.
const PAGE = readFileSync('../tests/fixtures/review-page.html', 'utf8')
const ROUTE = '/task/widget/42/review'
const NEWER = 'this review page needs a newer console'
const CHANGED = 'the plan changed; your selections were reset to the saved ones'

type Req = { kind: string; content: object; revision: string; answers: object }
const request = (text: string, revision = 'r1', answers: object = {}): Req => ({
  kind: 'plan-approval',
  content: { kind: 'readable', path: '.agent/review.html', media_type: 'text/html', text },
  revision,
  answers,
})

/** Mocks the request route and records every POST to the answers route. */
async function open(page: Page, req: () => Req, url = ROUTE) {
  const posts: Record<string, unknown>[] = []
  await page.route('**/api/task/widget/42/request', r => r.fulfill({ json: req() }))
  await page.route('**/api/task/widget/42/answers', r => r.fulfill({ status: 202, json: { status: 'pending', intent: 'x' } }))
  page.on('request', r => {
    if (r.method() === 'POST' && r.url().endsWith('/api/task/widget/42/answers')) posts.push(r.postDataJSON())
  })
  await page.goto(url)
  return posts
}
const frame = (page: Page) => page.frameLocator('[data-testid=review-frame]')
const pickTrack = (page: Page, name: string) => frame(page).getByText(name, { exact: true }).click()

test('the route shows only the sandboxed iframe, filling the viewport', async ({ page }) => {
  await open(page, () => request(PAGE))
  const iframe = page.getByTestId('review-frame')
  await expect(iframe).toHaveAttribute('sandbox', 'allow-scripts')
  await expect(iframe).toHaveAttribute('title', 'review')
  await expect(frame(page).locator('#send')).toBeVisible()
  await expect(page.getByTestId('request-panel')).toHaveCount(0)
  await expect(page.locator('nav')).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Artifacts' })).toHaveCount(0)
  const vp = page.viewportSize()!
  const box = (await iframe.boundingBox())!
  expect(box).toMatchObject({ x: 0, y: 0, width: vp.width, height: vp.height })
  expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(vp.width)
})

for (const scheme of ['light', 'dark'] as const) {
  test(`the page at 400px fits and matches the console (${scheme})`, async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await page.emulateMedia({ colorScheme: scheme })
    await open(page, () => request(PAGE))
    await expect(frame(page).locator('#send')).toBeVisible()
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(400)
    expect(await page.getByTestId('review-frame').boundingBox()).toMatchObject({ width: 400, height: 800 })
    const f = frame(page)
    const inner = await f.locator('html').evaluate(() => innerHeight)
    expect(await f.locator('.bar').evaluate(el => el.getBoundingClientRect().bottom)).toBe(inner)
    expect(await f.locator('html').evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(400)
    const outer = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    expect(await f.locator('body').evaluate(el => getComputedStyle(el).backgroundColor)).toBe(outer)
  })
}

test('the task page links to the review route and has no approve button', async ({ page }) => {
  await open(page, () => request(PAGE), '/task/widget/42')
  const panel = page.getByTestId('request-panel')
  await expect(panel.getByText('plan awaiting review')).toBeVisible()
  await expect(panel.getByRole('link', { name: 'open review' })).toHaveAttribute('href', ROUTE)
  await expect(page.getByRole('button', { name: /approve plan|tap again to approve/i })).toHaveCount(0)
})

test('a message from the console window itself is ignored', async ({ page }) => {
  const posts = await open(page, () => request(PAGE))
  await expect(frame(page).locator('#send')).toBeVisible()
  await page.evaluate(() => window.postMessage({ type: 'answers', v: 1, answers: { format: 'a' }, submit: 'approve' }, '*'))
  await page.waitForTimeout(1500)
  expect(posts).toEqual([])
})

test('a message with v 2 posts nothing and shows the newer-console notice', async ({ page }) => {
  const v2 = `<html><body><script>
    parent.postMessage({type:'ready',v:1},'*');
    setTimeout(()=>parent.postMessage({type:'answers',v:2,answers:{format:'a'},submit:null},'*'),300);
  </script></body></html>`
  const posts = await open(page, () => request(v2))
  await expect(page.getByTestId('review-notice')).toHaveText(NEWER)
  await page.waitForTimeout(1500)
  expect(posts).toEqual([])
})

test('after ready, exactly one restore arrives and the page shows it', async ({ page }) => {
  const counter = `<html><head><title>restores:0</title></head><body><script>
    let n = 0, got = null;
    addEventListener('message', e => { if (e.source === parent && e.data && e.data.type === 'restore') { n++; got = e.data; document.title = 'restores:' + n + ':' + e.data.v + ':' + JSON.stringify(e.data.answers) } });
    parent.postMessage({type:'ready',v:1},'*');
  </script></body></html>`
  await open(page, () => request(counter, 'r1', { format: 'b' }))
  const title = () => frame(page).locator('html').evaluate(() => document.title)
  await expect.poll(title).toBe('restores:1:1:{"format":"b"}')
  await page.waitForTimeout(1500)
  expect(await title()).toBe('restores:1:1:{"format":"b"}')
})

test('the real page shows the restored options', async ({ page }) => {
  await open(page, () => request(PAGE, 'r1', { format: 'b', track: 'security' }))
  const f = frame(page)
  await expect(f.locator('input[name=format][value=b]')).toBeChecked()
  await expect(f.locator('input[name=track][value=security]')).toBeChecked()
})

test('three selections in a second make one POST with the full set and the revision', async ({ page }) => {
  const posts = await open(page, () => request(PAGE, 'r7'))
  await frame(page).locator('input[name=format][value=a]').check()
  await frame(page).locator('input[name=format][value=b]').check()
  await pickTrack(page, 'security')
  expect(posts).toEqual([])
  await expect.poll(() => posts.length, { timeout: 3000 }).toBe(1)
  await page.waitForTimeout(1500)
  expect(posts).toEqual([{ answers: { format: 'b', track: 'security' }, submit: null, revision: 'r7' }])
})

test('a button press with a pending draft posts once, at once, as a submission', async ({ page }) => {
  const posts = await open(page, () => request(PAGE, 'r7'))
  await frame(page).locator('input[name=format][value=a]').check()
  await frame(page).locator('#send').click()
  await expect.poll(() => posts.length, { timeout: 500 }).toBe(1)
  await page.waitForTimeout(1500)
  expect(posts).toEqual([{ answers: { format: 'a', track: 'standard' }, submit: 'changes', revision: 'r7' }])
})

test('pagehide with a pending draft posts at once', async ({ page }) => {
  const posts = await open(page, () => request(PAGE, 'r7'))
  await frame(page).locator('input[name=format][value=a]').check()
  await page.evaluate(() => window.dispatchEvent(new Event('pagehide')))
  await expect.poll(() => posts.length, { timeout: 500 }).toBe(1)
  await page.waitForTimeout(1500)
  expect(posts).toEqual([{ answers: { format: 'a', track: 'standard' }, submit: null, revision: 'r7' }])
})

test('a new revision reloads the page, restores answers, and shows the notice', async ({ page }) => {
  let next = false
  const page2 = PAGE.replace('</body>', '<p id="rev">second page</p></body>')
  await open(page, () => (next ? request(page2, 'r2', { format: 'b' }) : request(PAGE, 'r1', { format: 'a' })))
  const f = frame(page)
  await expect(f.locator('input[name=format][value=a]')).toBeChecked()
  await expect(page.getByTestId('review-notice')).toHaveCount(0)
  next = true
  await expect(f.locator('#rev')).toHaveText('second page', { timeout: 20_000 })
  await expect(page.getByTestId('review-notice')).toHaveText(CHANGED)
  await expect(f.locator('input[name=format][value=b]')).toBeChecked()
})
