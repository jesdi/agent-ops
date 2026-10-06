import { expect, test } from '@playwright/test'

test('the nav opens the task states reference, and it links back to the board', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'Task states' }).click()
  await expect(page).toHaveURL(/\/task-states\.html$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Task states' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Board columns' })).toBeVisible()
  await page.getByRole('link', { name: 'Board', exact: true }).click()
  await expect(page).toHaveURL(/\/$/)
})
