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

/** Mocks the request and delivery routes and records every POST to the answers route. */
async function open(page: Page, req: () => Req | null, url = ROUTE, delivery: () => object | null = () => null) {
  const posts: Record<string, unknown>[] = []
  await page.route('**/api/task/widget/42/request', r => r.fulfill({ json: req() }))
  await page.route('**/api/task/widget/42/delivery', r => r.fulfill({ json: delivery() }))
  await page.route('**/api/task/widget/42/answers', r => r.fulfill({ status: 202, json: { status: 'pending', intent: 'x' } }))
  page.on('request', r => {
    if (r.method() === 'POST' && r.url().endsWith('/api/task/widget/42/answers')) posts.push(r.postDataJSON())
  })
  await page.goto(url)
  return posts
}
const frame = (page: Page) => page.frameLocator('[data-testid=review-frame]')
const pickTrack = (page: Page, name: string) => frame(page).getByText(name, { exact: true }).click()

test('the route shows only the sandboxed iframe and the submit bar, filling the viewport', async ({ page }) => {
  await open(page, () => request(PAGE))
  const iframe = page.getByTestId('review-frame')
  await expect(iframe).toHaveAttribute('sandbox', 'allow-scripts')
  await expect(iframe).toHaveAttribute('title', 'review')
  await expect(frame(page).locator('#state')).toBeVisible()
  await expect(page.getByTestId('request-panel')).toHaveCount(0)
  await expect(page.locator('nav')).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Artifacts' })).toHaveCount(0)
  const vp = page.viewportSize()!
  const box = (await iframe.boundingBox())!
  const bar = (await page.getByTestId('review-bar').boundingBox())!
  expect(box).toMatchObject({ x: 0, y: 0, width: vp.width })
  expect(bar).toMatchObject({ x: 0, y: box.height, width: vp.width })
  expect(box.height + bar.height).toBe(vp.height)
  // The buttons are the console's: the page has none.
  await expect(frame(page).locator('button')).toHaveCount(0)
  await expect(page.getByRole('button')).toHaveText(['Send changes', 'Approve'])
  expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(vp.width)
})

for (const scheme of ['light', 'dark'] as const) {
  test(`the page at 400px fits and matches the console (${scheme})`, async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await page.emulateMedia({ colorScheme: scheme })
    await open(page, () => request(PAGE))
    await expect(frame(page).locator('#state')).toBeVisible()
    expect(await page.evaluate('document.documentElement.scrollWidth')).toBeLessThanOrEqual(400)
    const bar = (await page.getByTestId('review-bar').boundingBox())!
    expect(bar.y + bar.height).toBe(800)
    expect(await page.getByTestId('review-frame').boundingBox()).toMatchObject({ width: 400, height: 800 - bar.height })
    for (const b of await page.getByRole('button').all()) expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(44)
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
  await expect(frame(page).locator('#state')).toBeVisible()
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
  await page.getByRole('button', { name: 'Send changes' }).click()
  await expect.poll(() => posts.length, { timeout: 500 }).toBe(1)
  await page.waitForTimeout(1500)
  expect(posts).toEqual([{ answers: { format: 'a', track: 'standard' }, submit: 'changes', revision: 'r7' }])
})

test('a page that posts submit approve by itself approves nothing', async ({ page }) => {
  const hostile = `<html><body><script>
    parent.postMessage({type:'ready',v:1},'*');
    parent.postMessage({type:'answers',v:1,answers:{format:'a'},submit:'approve'},'*');
  </script></body></html>`
  const posts = await open(page, () => request(hostile, 'r7'))
  await expect.poll(() => posts.length, { timeout: 3000 }).toBe(1)
  await page.waitForTimeout(1500)
  expect(posts).toEqual([{ answers: { format: 'a' }, submit: null, revision: 'r7' }])
})

test('Approve needs a second tap, then posts the selections as an approval', async ({ page }) => {
  const posts = await open(page, () => request(PAGE, 'r7'))
  await frame(page).locator('input[name=format][value=b]').check()
  await page.getByRole('button', { name: 'Approve' }).click()
  await page.getByRole('button', { name: 'Tap again to approve' }).click()
  await expect.poll(() => posts.length, { timeout: 500 }).toBe(1)
  await page.waitForTimeout(1500)
  expect(posts).toEqual([{ answers: { format: 'b', track: 'standard' }, submit: 'approve', revision: 'r7' }])
  await expect(page.getByTestId('review-notice')).toHaveText('approved; implement starts with these answers')
})

test('a questionnaire has one button, Send answers', async ({ page }) => {
  const posts = await open(page, () => ({ ...request(PAGE.replace('data-mode="plan"', 'data-mode="questionnaire"'), 'r7'), kind: 'answers' }))
  await expect(page.getByRole('button')).toHaveText(['Send answers'])
  await frame(page).locator('input[name=format][value=a]').check()
  await page.getByRole('button', { name: 'Send answers' }).click()
  await expect.poll(() => posts.length, { timeout: 500 }).toBe(1)
  expect(posts[0]).toMatchObject({ answers: { format: 'a' }, submit: 'changes', revision: 'r7' })
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

// Ticket 04: the Send bar shows the delivery, Resend, and the restore after a page change.
const PAGE_CHANGED = 'page changed, check and resend'

test('scenario 19: at 390x844 the bar with a drop and Resend fits, Send answers fully inside, no scroll', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  let delivery: object | null = null
  const posts = await open(page, () => ({ ...request(PAGE.replace('data-mode="plan"', 'data-mode="questionnaire"')), kind: 'answers' }), ROUTE, () => delivery)
  await frame(page).locator('input[name=format][value=b]').check()
  await page.getByRole('button', { name: 'Send answers' }).click()
  await expect.poll(() => posts.length, { timeout: 500 }).toBe(1)
  delivery = { state: 'dropped', reason: 'no open request', submit: 'changes', revision: 'r1' }
  await expect(page.getByTestId('review-delivery')).toHaveText(/dropped: no open request/, { timeout: 20_000 })
  // Scenario 14: a drop keeps the answers on the page.
  await expect(frame(page).locator('input[name=format][value=b]')).toBeChecked()
  for (const name of ['Send answers', 'Resend']) {
    const b = (await page.getByRole('button', { name }).boundingBox())!
    expect(b.x).toBeGreaterThanOrEqual(0)
    expect(b.y).toBeGreaterThanOrEqual(0)
    expect(b.x + b.width).toBeLessThanOrEqual(390)
    expect(b.y + b.height).toBeLessThanOrEqual(844)
    expect(b.height).toBeGreaterThanOrEqual(44)
  }
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(844)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
})

test('scenario 18: a stale-revision drop restores what still fits into the new page and submits nothing', async ({ page }) => {
  const scope = `<div class="q" data-q="scope"><header><h3>Scope?</h3></header><div class="opts">
    <label class="opt"><input type="radio" name="scope" value="narrow"><span>narrow</span></label>
    <label class="opt"><input type="radio" name="scope" value="wide"><span>wide</span></label></div></div>`
  const page1 = PAGE.replace('<!-- /slot -->', `${scope}\n    <!-- /slot -->`)
  const page2 = PAGE.replace('</body>', '<p id="rev">second page</p></body>')
  let next = false
  const posts = await open(page, () => (next ? request(page2, 'r2') : request(page1, 'r1')), ROUTE,
    () => (next ? { state: 'dropped', reason: 'stale revision', submit: 'changes', revision: 'r1' } : null))
  const f = frame(page)
  await f.locator('input[name=format][value=b]').check()
  await f.locator('input[name=scope][value=wide]').check()
  await f.locator('#note-format').fill('x')
  await page.getByRole('button', { name: 'Send changes' }).click()
  await expect.poll(() => posts.length, { timeout: 500 }).toBe(1)
  expect(posts[0]).toMatchObject({ answers: { format: 'b', scope: 'wide', 'format.note': 'x' }, submit: 'changes', revision: 'r1' })
  next = true
  await expect(f.locator('#rev')).toHaveText('second page', { timeout: 20_000 })
  await expect(page.getByText(PAGE_CHANGED)).toBeVisible({ timeout: 20_000 })
  await expect(f.locator('input[name=format][value=b]')).toBeChecked()
  await expect(f.locator('#note-format')).toHaveValue('x')
  await expect(f.locator('input[name=scope]')).toHaveCount(0)
  await page.waitForTimeout(1500)
  expect(posts.filter(p => p.submit !== null)).toHaveLength(1)
  expect(posts.every(p => !('scope' in (p.answers as object)) || p.revision === 'r1')).toBe(true)
})
