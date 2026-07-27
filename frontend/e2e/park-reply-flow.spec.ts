import { expect, test } from '@playwright/test'

test('park -> reply -> pending -> confirmed', async ({ page, request }) => {
  await page.goto('/')

  // A parked card is on the board.
  const parkedColumn = page.getByTestId('column-parked')
  await expect(parkedColumn.getByText('Fix login redirect')).toBeVisible()

  // Open the task and send a reply.
  await parkedColumn.getByText('Fix login redirect').click()
  await expect(page).toHaveURL(/\/task\/42$/)
  await page.getByLabel('Reply').fill('use the staging redirect URL')
  await page.getByRole('button', { name: 'Send reply' }).click()

  // Pending badge appears; task state is NOT optimistically flipped.
  await expect(page.getByTestId('pending-badge')).toBeVisible()
  await expect(page.getByText('parked: question')).toBeVisible()

  // The fake dispatcher applies the intent and pushes an SSE event.
  const res = await request.post('/__control__/apply-intents')
  expect(res.ok()).toBe(true)

  // Badge clears once board data confirms the state change.
  await expect(page.getByTestId('pending-badge')).toBeHidden()

  // The card moved to in-progress on the board.
  await page.goto('/')
  await expect(
    page.getByTestId('column-in-progress').getByText('Fix login redirect'),
  ).toBeVisible()
  await expect(
    page.getByTestId('column-parked').getByText('Fix login redirect'),
  ).toBeHidden()
})
