import { expect, test } from '@playwright/test'

// A cancel records a pending intent in the fake server; clear it both ways so
// neither this spec nor a later one sees a stray badge.
test.beforeEach(async ({ request }) => {
  await request.post('/__control__/reset-messages')
})
test.afterEach(async ({ request }) => {
  await request.post('/__control__/reset-messages')
})

test('empty columns are chips and the Wont do chip takes a drop', async ({ page }) => {
  await page.goto('/')
  const strip = page.getByRole('list', { name: 'Empty columns' })
  await expect(strip.getByRole('listitem', { name: 'Failed: 0' })).toBeVisible()
  await expect(page.getByTestId('column-failed')).toHaveCount(0)
  // Occupied columns stay in the row.
  await expect(page.getByTestId('column-parked')).toBeVisible()
  await expect(page.getByTestId('column-queued')).toBeVisible()

  await page.getByTestId('card-42').dragTo(page.getByTestId('chip-wont-do'))
  await expect(page.getByTestId('wont-do-confirm')).toContainText('#42')
  await page.getByRole('button', { name: "Confirm won't do?" }).click()
  await expect(page.getByTestId('wont-do-confirm')).toHaveCount(0)
  await expect(page.getByTestId('card-42').getByText('pending: cancel')).toBeVisible()
})
