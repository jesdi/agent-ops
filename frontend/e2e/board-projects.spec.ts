import { expect, test } from '@playwright/test'

// Spec S23: a paused project with no tasks has its own tab, showing it paused.
test('a paused project without work has its own tab and says it is paused', async ({ page }) => {
  await page.goto('/')
  const tabs = page.getByRole('tablist', { name: 'Projects' })
  await expect(tabs.getByRole('tab', { name: 'widget' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByTestId('card-42')).toBeAttached()

  await tabs.getByRole('tab', { name: /gadgets/ }).click()
  await expect(page).toHaveURL(/[?&]project=gadgets/)
  await expect(tabs.getByRole('tab', { name: /gadgets/ })).toContainText('Paused')
  await expect(page.getByText('gadgets is paused: new issues are not claimed')).toBeVisible()
  await expect(page.getByTestId('card-42')).toHaveCount(0)
  await expect(page.getByTestId('ghost-73')).toHaveCount(0)
  await page.screenshot({ path: test.info().outputPath('paused-project.png') })

  const width = page.viewportSize()!.width
  expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(width)
})
