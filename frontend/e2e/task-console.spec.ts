import { expect, test } from '@playwright/test'
import type { Locator } from '@playwright/test'

const gapToBottom = (pane: Locator) =>
  pane.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)

test('the task console opens on its last line and has a jump-to-bottom control', async ({ page }) => {
  await page.goto('/task/widget/42')
  const pane = page.getByTestId('pane-tail')
  await expect(pane).toContainText('? Should I use the staging redirect URL or prod?')
  await expect.poll(() => gapToBottom(pane)).toBeLessThanOrEqual(4)

  const jump = page.getByRole('button', { name: 'Scroll to bottom' })
  await expect(jump).toBeHidden()

  await pane.evaluate((el) => el.scrollTo({ top: 0 }))
  await expect(jump).toBeVisible()
  await jump.click()
  await expect.poll(() => gapToBottom(pane)).toBeLessThanOrEqual(4)
  await expect(jump).toBeHidden()
})
