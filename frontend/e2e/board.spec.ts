import { expect, test } from '@playwright/test'
import { showColumn } from './phone.js'

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
  await showColumn(page, 'queued')
  await expect(page.getByTestId('column-queued')).toBeVisible()

  await showColumn(page, 'parked')
  await page.getByTestId('card-42').dragTo(page.getByTestId('chip-wont-do'))
  await expect(page.getByTestId('wont-do-confirm')).toContainText('#42')
  await page.getByRole('button', { name: "Confirm won't do?" }).click()
  await expect(page.getByTestId('wont-do-confirm')).toHaveCount(0)
  await expect(page.getByTestId('card-42').getByText('pending: cancel')).toBeVisible()
})

test('a card expands and collapses in place', async ({ page }) => {
  await page.goto('/')
  const card = page.getByTestId('card-42')
  const toggle = card.getByRole('button', { name: 'Details for widget#42' })
  await expect(card.getByText('sonnet')).toHaveCount(0)
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(card.getByText('sonnet')).toBeVisible()
  await toggle.click()
  await expect(card.getByText('sonnet')).toHaveCount(0)
})

test('phone: tapping the card body expands it instead of navigating', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'touch only')
  await page.goto('/')
  const card = page.getByTestId('card-42')
  await card.getByText('widget#42').tap()
  await expect(card.getByText('sonnet')).toBeVisible()
  await expect(page).toHaveURL(/\/$/)
})

test('phone: one tab per occupied column, Needs you first, with counts', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'the tab row is the phone layout')
  await page.goto('/')
  const tabs = page.getByRole('tablist', { name: 'Columns' }).getByRole('tab')
  await expect(tabs).toHaveText(['Parked 1', 'Queued 2'])
  // No parameter: the first occupied column is active and alone on screen.
  await expect(tabs.first()).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByTestId('column-parked')).toBeVisible()
  await expect(page.getByTestId('column-queued')).toBeHidden()
  // Empty columns stay chips under the tabs.
  await expect(page.getByRole('list', { name: 'Empty columns' }).getByRole('listitem', { name: 'Failed: 0' })).toBeVisible()
  expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(page.viewportSize()!.width)
})

test('phone: the selected tab lives in the URL; reload and back restore it', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'the tab row is the phone layout')
  await page.goto('/')
  const queuedTab = page.getByRole('tab', { name: 'Queued 2' })
  await queuedTab.click()
  await expect(page).toHaveURL(/\?column=queued$/)
  await expect(queuedTab).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByTestId('column-queued')).toBeVisible()
  await expect(page.getByTestId('column-parked')).toBeHidden()

  await page.reload()
  await expect(queuedTab).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByTestId('column-queued')).toBeVisible()

  await page.getByTestId('column-queued').getByText('Ship dark mode').click()
  await expect(page).toHaveURL(/\/task\/widget\/73$/)
  await page.goBack()
  await expect(queuedTab).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByTestId('column-queued')).toBeVisible()

  // Arrow keys move the selection along the row.
  await queuedTab.focus()
  await page.keyboard.press('ArrowLeft')
  await expect(page).toHaveURL(/\?column=parked$/)
  await expect(page.getByRole('tab', { name: 'Parked 1' })).toBeFocused()
})

test('phone: the header is one summary line; the disclosure reveals the rest', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'the desktop header is always open')
  await page.goto('/')
  const summary = page.getByRole('button', { name: /active/ })
  await expect(summary).toHaveText(/^\d+\/\d+ active · will claim #73$/)
  await expect(summary).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByText('≈2h per task')).toBeHidden()
  await expect(page.getByTestId('next-claim')).toBeHidden()
  await summary.click()
  await expect(summary).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByText('≈2h per task')).toBeVisible()
  await expect(page.getByTestId('next-claim')).toBeVisible()
  await expect(page.getByRole('progressbar', { name: /Session · 5h used/ }).first()).toBeVisible()
})

test('desktop: no tab row and no summary line', async ({ page, isMobile }) => {
  test.skip(isMobile, 'desktop only')
  await page.goto('/')
  await expect(page.getByTestId('column-queued')).toBeVisible()
  await expect(page.getByRole('tablist')).toBeHidden()
  await expect(page.getByRole('button', { name: /active/ })).toBeHidden()
  await expect(page.getByText('≈2h per task')).toBeVisible()
})
