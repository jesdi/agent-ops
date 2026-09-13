import { expect, test } from '@playwright/test'

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`saved cards render before live checks at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport)
    let finish!: () => void
    const pending = new Promise<void>((resolve) => { finish = resolve })
    await page.route('**/api/board', async (route) => {
      await pending
      await route.continue()
    })
    await page.goto('/')
    try {
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
}
