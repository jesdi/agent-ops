import { expect, test } from '@playwright/test'
import { routeLongBoard } from './long-board.js'

test.beforeEach(async ({ request }) => {
  await request.post('/__control__/reset-queue')
})

test('desktop board fits the viewport; columns scroll on their own', async ({ page, isMobile }) => {
  test.skip(isMobile, 'the phone layout is one column at a time')
  await routeLongBoard(page)
  await page.goto('/')
  const body = page.getByTestId('scroll-queued')
  await expect(body.getByTestId('ghost-239')).toBeAttached()

  const viewport = page.viewportSize()!
  // No page scroll, however long a column is.
  expect(await page.evaluate<number>('document.documentElement.scrollHeight')).toBeLessThanOrEqual(viewport.height)
  expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(viewport.width)

  // The row overflows sideways and its bottom edge (the scrollbar) is on screen.
  const row = page.getByTestId('board-row')
  expect(await row.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true)
  const rowBox = (await row.boundingBox())!
  expect(rowBox.y + rowBox.height).toBeLessThanOrEqual(viewport.height)

  // Queued scrolls on its own; its header and its neighbours stay put.
  // Bring it into view sideways first: the Needs you zone fills 1280px.
  await page.getByTestId('column-queued').scrollIntoViewIfNeeded()
  expect(await body.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true)
  const header = page.getByTestId('column-queued').getByText('Queued', { exact: true })
  const headerTop = (await header.boundingBox())!.y
  const neighbour = page.getByTestId('column-in-progress').locator('article').first()
  const neighbourTop = (await neighbour.boundingBox())!.y
  await expect(body.getByTestId('ghost-239')).not.toBeInViewport()
  await body.evaluate((el) => el.scrollTo(0, el.scrollHeight))
  await expect(body.getByTestId('ghost-239')).toBeInViewport()
  await expect(header).toBeInViewport()
  expect((await header.boundingBox())!.y).toBe(headerTop)
  expect((await neighbour.boundingBox())!.y).toBe(neighbourTop)
  expect(await page.evaluate<number>('window.scrollY')).toBe(0)
})
