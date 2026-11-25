import { expect, test, type Page } from '@playwright/test'

// The whole exam page against the fake API (pr-exam-host-assist requirement 12).
// Interface under test (defined here, built into e2e/fake-api.mjs by the ticket):
//   after POST /__control__/reset the fake serves, at /api/exam/jesdi/general-skills/240, an open attempt 1 with
//   q1 (choice "Which SHA does the lease compare?", options "the head SHA" / "the base SHA", answer: the base SHA)
//   and q2 (text "Why is the base SHA rechecked before merging?");
//   POST .../turns {kind:'tip'|'question'|'general', ...} starts a turn and its stream answers
//   a tip with "Look at what the lease stores.", a question with "Because main may have moved.",
//   a general message with "The lease expiry."; the fake runs the real route set (turns, stream, bypass, answers);
//   POST .../answers moves the attempt to grading, then to a failed result that gives q2 half the points with the
//   feedback "misses that main may have moved" (a bypassed question is left out of the result's points and shows "bypassed").
const PAGE = '/exam/jesdi/general-skills/240'
const Q1 = 'Which SHA does the lease compare?'
const Q2 = 'Why is the base SHA rechecked before merging?'
const questionOf = (page: Page, prompt: string) => page.locator('li', { hasText: prompt }).last()
const drawer = (page: Page) => page.getByRole('complementary', { name: 'General chat' })

test.beforeEach(async ({ request }) => {
  await request.post('/__control__/reset')
})

async function select(page: Page, text: string) {
  await page.evaluate((needle) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const at = n.textContent?.indexOf(needle) ?? -1
      if (at < 0 || n.parentElement?.closest('aside')) continue
      const range = document.createRange()
      range.setStart(n, at)
      range.setEnd(n, at + needle.length)
      const selection = document.getSelection()!
      selection.removeAllRanges()
      selection.addRange(range)
      document.dispatchEvent(new Event('selectionchange'))
      return
    }
    throw new Error(`no text ${needle}`)
  }, text)
}

test('a tip and a question thread stream their replies into the question', async ({ page }) => {
  await page.goto(PAGE)
  const q2 = questionOf(page, Q2)
  await q2.getByRole('button', { name: /^tip/i }).click()
  await expect(q2.getByText('Look at what the lease stores.')).toBeVisible()
  await expect(q2.getByText(/assisted/)).toBeVisible()
  await q2.getByRole('button', { name: /^ask$/i }).click()
  await q2.getByRole('textbox', { name: /message/i }).fill('why?')
  await q2.getByRole('button', { name: /^send$/i }).click()
  await expect(q2.getByText('Because main may have moved.')).toBeVisible()
})

test('a general chat message streams a reply in the chat', async ({ page, request }) => {
  await request.post('/__control__/hold-turns')
  await page.goto(PAGE)
  await page.getByRole('button', { name: 'Chat', exact: true }).click()
  await drawer(page).getByRole('textbox', { name: /message/i }).fill('what is the riskiest change?')
  await drawer(page).getByRole('button', { name: /^send$/i }).click()
  // The turn is held: its stored reply is empty, so only the streamed delta can put text in the chat.
  await expect(drawer(page).getByText('The lease', { exact: true })).toBeVisible()
  await expect(drawer(page).getByText('The lease expiry.')).toHaveCount(0)
  await request.post('/__control__/release-turns')
  await expect(drawer(page).getByText('The lease expiry.')).toBeVisible()
})

test('the general chat is a bottom sheet at 390 px with no horizontal scroll, even with a long diff reply', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'the phone project covers the narrow screen')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(PAGE)
  await page.getByRole('button', { name: 'Chat', exact: true }).click()
  const sheet = drawer(page)
  await sheet.getByRole('textbox', { name: /message/i }).fill('show the long diff')
  await sheet.getByRole('button', { name: /^send$/i }).click()
  await expect(sheet.getByText('leaseCompareHeadShaAgainstBaseShaBeforeMerging', { exact: false }).first()).toBeVisible()
  const box = (await sheet.boundingBox())!
  expect(box.x).toBe(0)
  expect(box.width).toBe(390)
  expect(box.y + box.height).toBe(844)
  expect(box.height).toBeLessThan(844)
  expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
})

test('selecting text quotes it into the question thread, or into the general chat from the header', async ({ page }) => {
  await page.goto(PAGE)
  await expect(page.getByText(Q1)).toBeVisible()
  await select(page, 'the head SHA')
  await expect(questionOf(page, Q1).getByRole('textbox', { name: /message/i })).toHaveValue('> the head SHA\n\n')
  await select(page, 'Exam for')
  await expect(drawer(page).getByRole('textbox', { name: /message/i })).toHaveValue('> Exam for\n\n')
})

test('a bypassed question is left unanswered, and the result shows it as bypassed', async ({ page }) => {
  page.on('dialog', (dialog) => dialog.accept())
  await page.goto(PAGE)
  await questionOf(page, Q2).getByRole('button', { name: /^bypass$/i }).click()
  await expect(questionOf(page, Q2).getByRole('button', { name: /undo bypass/i })).toBeVisible()
  await page.getByRole('radio', { name: 'the base SHA' }).check()
  await page.getByRole('button', { name: /submit/i }).click()
  await expect(page.getByText(/bypassed/i).first()).toBeVisible({ timeout: 15000 })
})

test('a submit ends in a result with the grader feedback', async ({ page }) => {
  await page.goto(PAGE)
  await page.getByRole('radio', { name: 'the base SHA' }).check()
  await page.getByRole('textbox', { name: Q2 }).fill('Because main may have moved.')
  await page.getByRole('button', { name: /submit/i }).click()
  await expect(page.getByText(/misses that main may have moved/)).toBeVisible({ timeout: 15000 })
})
