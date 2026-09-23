import { expect, test } from '@playwright/test'
import { showHeader } from './phone.js'

test('saved cards render before live checks', async ({ page }) => {
  let finish!: () => void
  const pending = new Promise<void>((resolve) => { finish = resolve })
  await page.route('**/api/board', async (route) => {
    await pending
    await route.continue()
  })
  await page.goto('/')
  try {
    await showHeader(page)
    await expect(page.getByText('loading queue and forecast…')).toBeVisible()
    const card = page.getByTestId('card-42')
    await card.scrollIntoViewIfNeeded()
    await expect(card).toBeVisible()
  } finally {
    finish()
  }
  await expect(page.getByText('loading queue and forecast…')).toHaveCount(0)
  await expect(page.getByTestId('ghost-73')).toBeAttached()
  await page.getByTestId('card-42').getByText('Fix login redirect').click()
  await expect(page).toHaveURL(/\/task\/widget\/42$/)
  await expect(page.getByRole('heading', { name: /Fix login redirect/ })).toBeVisible()
})
