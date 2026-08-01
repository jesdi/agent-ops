import { expect, test } from '@playwright/test'

test('ghosts -> slim view -> description -> boost reorders', async ({ page }) => {
  await page.goto('/')

  // Header: next-claim line and median cycle.
  await expect(page.getByTestId('next-claim')).toContainText('will claim #73')
  await expect(page.getByText('≈2h per task')).toBeVisible()

  // Queued column holds the ranked ghosts in order; head carries the badge.
  const queued = page.getByTestId('column-queued')
  const ghosts = queued.locator('[data-testid^="ghost-"]')
  await expect(ghosts).toHaveCount(2)
  await expect(ghosts.first()).toContainText('Ship dark mode')
  await expect(ghosts.first().getByText('next', { exact: true })).toBeVisible()

  // Open the slim view: description is expanded, body rendered.
  await queued.getByText('Ship dark mode').click()
  await expect(page).toHaveURL(/\/task\/73$/)
  await expect(page.getByTestId('ghost-task-view')).toBeVisible()
  await expect(page.getByText('Body of issue 73.')).toBeVisible()

  // Boost #74 from the board; order flips and the badge moves.
  await page.goto('/')
  await queued.locator('[data-testid="ghost-74"]').getByRole('button', { name: 'Boost' }).click()
  await expect(ghosts.first()).toContainText('Fix flaky test')
  await expect(page.getByTestId('next-claim')).toContainText('will claim #74')
})
