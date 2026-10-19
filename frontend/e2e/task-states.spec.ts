import { expect, test } from '@playwright/test'

test('the nav opens the task states diagram', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'Task states' }).click()
  await expect(page).toHaveURL(/\/task-states\.html$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Task states' })).toBeVisible()
})
