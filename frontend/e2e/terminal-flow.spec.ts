import { expect, test } from '@playwright/test'

test('open task -> attach terminal -> see output', async ({ page }) => {
  await page.goto('/task/42')
  await expect(page.getByText('Fix login redirect')).toBeVisible()

  await page.getByRole('button', { name: 'Attach terminal' }).click()
  await expect(page.getByTestId('terminal')).toBeVisible()

  // The fake WS server sent bytes on connect; xterm's DOM renderer
  // paints them into .xterm-rows.
  await expect(page.locator('.xterm-rows')).toContainText(
    'hello from task 42',
  )
})
