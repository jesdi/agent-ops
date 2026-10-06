import { expect, test, type Page } from '@playwright/test'

const TEXT = '12/12 tickets merged, last review 2 left' // exactly 40 characters
const URL = '/task/widget/42'

async function serve(page: Page, progress: string | null) {
  await page.route('**/api/task/widget/42', async route => {
    const res = await route.fetch()
    const body = await res.json()
    await route.fulfill({ json: { ...body, implement_progress: progress } })
  })
}

test('the task page shows the implement progress', async ({ page }) => {
  await serve(page, '2/4 tickets merged')
  await page.goto(URL)
  await expect(page.getByTestId('implement-progress')).toHaveText('2/4 tickets merged')
})

test('no progress line when the session reported none', async ({ page }) => {
  // Not vacuous: the same page shows a line first, then none once it is null.
  await serve(page, '2/4 tickets merged')
  await page.goto(URL)
  await expect(page.getByTestId('implement-progress')).toBeVisible()
  await page.unroute('**/api/task/widget/42')
  await serve(page, null)
  await page.reload()
  await expect(page.getByText('Fix login redirect').first()).toBeVisible()
  await expect(page.getByTestId('implement-progress')).toHaveCount(0)
})

type Box = { top: number, left: number, width: number, height: number }

// Box of the element at a DOM index path (child indices from <body>).
const boxAt = (page: Page, path: number[]) => page.evaluate((path) => {
  let el: Element = document.body
  for (const i of path) el = el.children[i]
  const r = el.getBoundingClientRect()
  return { top: r.top + scrollY, left: r.left, width: r.width, height: r.height }
}, path) as Promise<Box>

for (const width of [390, 360]) {
  test(`a 40-character progress fits at ${width}px without layout shift`, async ({ page }) => {
    expect(TEXT).toHaveLength(40)
    await page.setViewportSize({ width, height: 900 })
    await serve(page, TEXT)
    await page.goto(URL)
    const line = page.getByTestId('implement-progress')
    await expect(line).toHaveText(TEXT)
    // DOM index paths of the line's neighbours.
    const [prevPath, nextPath] = await line.evaluate(el => {
      const path = (e: Element) => {
        const out: number[] = []
        for (; e !== document.body; e = e.parentElement!)
          out.unshift([...e.parentElement!.children].indexOf(e))
        return out
      }
      return [path(el.previousElementSibling!), path(el.nextElementSibling!)]
    })
    const prevWith = await boxAt(page, prevPath)
    const nextWith = await boxAt(page, nextPath)
    const m = await line.evaluate(el => ({
      right: el.getBoundingClientRect().right, height: el.getBoundingClientRect().height,
      scrollW: el.scrollWidth, clientW: el.clientWidth,
    }))
    // No overflow, side gutter kept, text whole.
    expect(await page.evaluate(() =>
      document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
    expect(m.right).toBeLessThanOrEqual(width - 16)
    expect(m.scrollW).toBeLessThanOrEqual(m.clientW)

    // Same page without progress: the two neighbours now touch.
    await page.unroute('**/api/task/widget/42')
    await serve(page, null)
    await page.reload()
    await expect(page.getByText('Fix login redirect').first()).toBeVisible()
    await expect(line).toHaveCount(0)
    const nextPathWithout = [...nextPath.slice(0, -1), nextPath[nextPath.length - 1] - 1]
    expect(await boxAt(page, prevPath)).toEqual(prevWith)   // above: not moved at all
    const nextWithout = await boxAt(page, nextPathWithout)
    expect(nextWithout.left).toBe(nextWith.left)             // below: no horizontal reflow
    expect(nextWithout.width).toBe(nextWith.width)
    const shift = nextWith.top - nextWithout.top
    expect(shift).toBeGreaterThanOrEqual(m.height)           // down by the line...
    expect(shift - m.height).toBeLessThanOrEqual(24)         // ...plus its spacing only
  })
}
