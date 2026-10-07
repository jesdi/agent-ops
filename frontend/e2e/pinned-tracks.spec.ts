/// <reference lib="dom" />
import { expect, test, type Locator, type Page } from '@playwright/test'

// Pinned tracks, ticket 03: the line under the priority control and the pin on
// a task card fit at phone and desktop width, light and dark.

test.beforeEach(async ({ request }) => {
  await request.post('/__control__/reset-queue')
  await request.post('/__control__/seed-pinned')
})

const viewports = [{ name: 'phone', width: 390, height: 844 }, { name: 'desktop', width: 1280, height: 800 }]

const noPageScroll = (page: Page) =>
  page.evaluate<boolean>('document.documentElement.scrollWidth <= document.documentElement.clientWidth')

/** Every text-bearing element under `root` is inside the viewport, unclipped. */
async function expectNoClip(root: Locator, width: number) {
  const bad = await root.evaluate((el, vw) => {
    const out: string[] = []
    for (const node of [el, ...el.querySelectorAll('*')] as HTMLElement[]) {
      if (!node.textContent?.trim()) continue
      const r = node.getBoundingClientRect()
      if (r.width === 0) continue
      if (r.left < -0.5 || r.right > vw + 0.5) out.push(`outside viewport: ${node.tagName} ${node.textContent.slice(0, 30)}`)
      if (node.scrollWidth > node.clientWidth + 1 && getComputedStyle(node).display !== 'inline') out.push(`clipped: ${node.tagName} ${node.textContent.slice(0, 30)}`)
    }
    return out
  }, width)
  expect(bad).toEqual([])
}

/** The two boxes share no area. */
async function expectNoOverlap(a: Locator, b: Locator) {
  const [x, y] = [(await a.boundingBox())!, (await b.boundingBox())!]
  const apart = x.x + x.width <= y.x + 0.5 || y.x + y.width <= x.x + 0.5
    || x.y + x.height <= y.y + 0.5 || y.y + y.height <= x.y + 0.5
  expect(apart).toBe(true)
}

for (const vp of viewports) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${vp.name} ${colorScheme}`, () => {
      test.beforeEach(async ({ page }) => {
        await page.emulateMedia({ colorScheme })
        await page.setViewportSize({ width: vp.width, height: vp.height })
      })

      test('the pinned-tracks line under the priority control fits', async ({ page }) => {
        await page.goto('/')
        // 390px folds the header on every project; wider shows it already.
        const fold = page.getByRole('button', { name: /active/, expanded: false })
        if (await fold.count()) await fold.click()
        const control = page.getByRole('radiogroup', { name: /priority/i })
        const line = page.getByText(/does not apply/i)
        await expect(line).toBeVisible()
        await expect(line).toContainText(/security.*architecture.*frontend/i)
        expect(await noPageScroll(page)).toBe(true)
        await expectNoClip(line, vp.width)
        await expectNoOverlap(control, line)
        const [c, l] = [(await control.boundingBox())!, (await line.boundingBox())!]
        expect(l.y).toBeGreaterThanOrEqual(c.y + c.height - 0.5) // under the control
      })

      test('the pin on a task card fits, expanded', async ({ page }) => {
        await page.goto('/')
        if (vp.name === 'phone') await page.locator('#tab-in-progress').click()
        const card = page.getByTestId('card-51')
        await expect(card).toBeVisible()
        await card.getByRole('button', { name: 'Details for widget#51' }).click()
        const pin = card.getByText(/pinned/i).first()
        await expect(pin).toBeVisible()
        await expect(pin).toContainText(/frontend/i)
        expect(await noPageScroll(page)).toBe(true)
        await expectNoClip(card, vp.width)
        await expectNoOverlap(pin, card.getByText('claude-fable-5-1', { exact: true }))
      })

      test('a pinned wait reads and fits', async ({ page }) => {
        await page.goto('/')
        if (vp.name === 'phone') await page.locator('#tab-parked').click()
        const card = page.getByTestId('card-52')
        await expect(card).toBeVisible()
        await card.getByRole('button', { name: /capacity limited/i }).click()
        const dialog = card.getByRole('dialog', { name: /model capacity options/i })
        await expect(dialog).toContainText(/pinned to (the )?frontend/i)
        await expect(dialog.getByRole('button', { name: /gpt-astra/ })).toBeVisible()
        expect(await noPageScroll(page)).toBe(true)
        await expectNoClip(dialog, vp.width)
      })
    })
  }
}
