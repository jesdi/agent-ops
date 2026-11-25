import { expect, test } from '@playwright/test'

// The general chat on a narrow screen (pr-exam-host-assist requirement 12): a bottom sheet, and no
// horizontal scroll. The exam view is routed here; the fake backend has no exam.
const SHA = 'a'.repeat(40)
const question = (id: string, prompt: string) => ({ id, type: 'text', prompt, tips: 0, assisted: false, thread: [] })
const view = {
  repo: 'jesdi/general-skills', pr: 240, state: 'open', head_sha: SHA, debt: false, detail: 'attempt 1, 2 questions',
  attempt: 1, bypassed: null, attempts: [],
  exam: {
    version: 1, repo: 'jesdi/general-skills', pr: 240, head_sha: SHA,
    questions: [question('q1', 'Why does the lease compare the head SHA?'), question('q2', 'Why is the base SHA rechecked before merging?')],
    general: [{ id: 'g1', kind: 'general', message: 'what is the riskiest change?', text: 'The lease expiry.', status: 'done', error: null }],
  },
}

test('the general chat is a bottom sheet at 390 px with no horizontal scroll', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'the phone project covers the narrow screen')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.route('**/api/exam/jesdi/general-skills/240', (route) => route.fulfill({ json: view }))
  await page.goto('/exam/jesdi/general-skills/240')
  await page.getByRole('button', { name: 'Chat', exact: true }).click()
  const sheet = page.getByRole('complementary', { name: 'General chat' })
  await expect(sheet.getByText('The lease expiry.')).toBeVisible()
  const box = (await sheet.boundingBox())!
  expect(box.x).toBe(0)
  expect(box.width).toBe(390)
  expect(box.y + box.height).toBe(844)
  expect(box.height).toBeLessThan(844)
  expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
})

test('the general chat is a drawer on the right on a desktop', async ({ page, isMobile }) => {
  test.skip(isMobile, 'the desktop project covers the drawer')
  await page.route('**/api/exam/jesdi/general-skills/240', (route) => route.fulfill({ json: view }))
  await page.goto('/exam/jesdi/general-skills/240')
  await page.getByRole('button', { name: 'Chat', exact: true }).click()
  const drawer = (await page.getByRole('complementary', { name: 'General chat' }).boundingBox())!
  const viewport = page.viewportSize()!
  expect(drawer.x + drawer.width).toBe(viewport.width)
  expect(drawer.height).toBe(viewport.height)
})
