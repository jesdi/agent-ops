import { test, type Page } from '@playwright/test'

// Phones show one board column at a time and fold the header into a summary
// line. These bring the part a flow needs on screen, and do nothing on
// desktop, where it all already is.

// The running project decides, not a copy of the md breakpoint.
const isPhone = () => test.info().project.use.isMobile === true

/** Selects the column's tab. */
export async function showColumn(page: Page, key: string) {
  if (!isPhone()) return
  await page.locator(`#tab-${key}`).click()
}

/** Opens the full header row behind the summary line. */
export async function showHeader(page: Page) {
  if (!isPhone()) return
  const summary = page.getByRole('button', { name: /active/, expanded: false })
  await summary.click()
}
