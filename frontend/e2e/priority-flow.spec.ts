import { expect, test } from '@playwright/test'
import { showHeader } from './phone.js'

test.beforeEach(async ({ request }) => {
  await request.post('/__control__/reset-queue')
})

const control = (page: import('@playwright/test').Page) => page.getByRole('radiogroup', { name: /priority/i })

test('choosing OpenAI sticks across a reload and puts the first chip on openai', async ({ page }) => {
  await page.goto('/')
  await showHeader(page)
  await expect(control(page).getByRole('radio', { name: 'Auto' })).toBeChecked()
  await control(page).getByRole('radio', { name: 'OpenAI' }).click()
  await expect(control(page).getByRole('radio', { name: 'OpenAI' })).toBeChecked()

  await page.reload()
  await showHeader(page)
  await expect(control(page).getByRole('radio', { name: 'OpenAI' })).toBeChecked()
  const chip = page.getByText('first', { exact: true })
  await expect(chip).toHaveCount(1)
  // The innermost block holding the chip and the openai heading, not anthropic's.
  const column = page.locator('div').filter({ has: chip }).filter({ hasText: /openai/i })
    .filter({ hasNotText: /anthropic/i })
  await expect(column.last()).toBeVisible()
})

for (const colorScheme of ['light', 'dark'] as const) {
  test(`at 390px the priority control fits, no horizontal scroll (${colorScheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/')
    await showHeader(page)
    const box = (await control(page).boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(390)
    expect(await page.evaluate<boolean>('document.documentElement.scrollWidth <= document.documentElement.clientWidth')).toBe(true)
  })
}
