import { expect, test } from '@playwright/test'

test('park -> reply -> pending -> confirmed', async ({ page, request }) => {
  // Restore the seed so this spec is idempotent across Playwright retries and
  // across the message-queue-flow spec that runs before it alphabetically.
  await request.post('/__control__/reset-messages')
  await page.goto('/')

  // A parked card is on the board.
  const parkedColumn = page.getByTestId('column-parked')
  await expect(parkedColumn.getByText('Fix login redirect')).toBeVisible()

  // Open the task and send a reply.
  await parkedColumn.getByText('Fix login redirect').click()
  await expect(page).toHaveURL(/\/task\/widget\/42$/)
  await page.getByLabel('Reply').fill('use the staging redirect URL')
  await page.getByRole('button', { name: 'Send reply & wake' }).click()

  // Pending badge appears; task state is NOT optimistically flipped.
  await expect(page.getByTestId('pending-badge')).toBeVisible()
  await expect(page.getByText('parked: question')).toBeVisible()

  // The fake dispatcher drains the intent into the durable queue but does NOT
  // resume yet — the task is starved (wake_blocked).
  const res = await request.post('/__control__/apply-intents')
  expect(res.ok()).toBe(true)

  // Badge clears once the intent is drained.
  await expect(page.getByTestId('pending-badge')).toBeHidden()

  // A slot frees: the parked task resumes and the card moves to in-progress.
  await request.post('/__control__/free-slot')

  // The card moved to in-progress on the board.
  await page.goto('/')
  await expect(
    page.getByTestId('column-in-progress').getByText('Fix login redirect'),
  ).toBeVisible()
  await expect(
    page.getByTestId('column-parked').getByText('Fix login redirect'),
  ).toBeHidden()
})
