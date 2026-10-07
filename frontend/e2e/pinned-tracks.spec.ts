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

/** No text under `root` is cut off: skips screen-reader text, and flags an
 *  element only when its content overflows AND the overflow is hidden, or its
 *  box leaves the viewport or `root`'s box. */
async function expectNoClip(root: Locator, width: number) {
  const bad = await root.evaluate((el, vw) => {
    const out: string[] = []
    const box = el.getBoundingClientRect()
    for (const node of [el, ...el.querySelectorAll('*')] as HTMLElement[]) {
      if (node.closest('.sr-only') || !node.textContent?.trim()) continue
      const r = node.getBoundingClientRect()
      if (r.width === 0) continue
      const label = `${node.tagName} ${node.textContent.slice(0, 30)}`
      if (r.left < -0.5 || r.right > vw + 0.5) out.push(`outside viewport: ${label}`)
      if (r.left < box.left - 0.5 || r.right > box.right + 0.5) out.push(`outside container: ${label}`)
      const cs = getComputedStyle(node)
      const hidden = ['hidden', 'clip'].includes(cs.overflowX)
      if (hidden && node.scrollWidth > node.clientWidth + 1) out.push(`clipped: ${label}`)
    }
    return out
  }, width)
  expect(bad).toEqual([])
}

/** The pin's whole text is visible, unclipped, inside the card and the viewport. */
async function expectPinFits(pin: Locator, card: Locator, width: number) {
  await expect(pin).toBeVisible()
  await expect(pin).toContainText(/pinned/i)
  await expect(pin).toContainText(/frontend/i)
  const [p, c] = [(await pin.boundingBox())!, (await card.boundingBox())!]
  expect(p.x).toBeGreaterThanOrEqual(c.x - 0.5)
  expect(p.x + p.width).toBeLessThanOrEqual(c.x + c.width + 0.5)
  expect(p.x).toBeGreaterThanOrEqual(-0.5)
  expect(p.x + p.width).toBeLessThanOrEqual(width + 0.5)
  expect(await pin.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
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
        // The fold needs the board; wait for it before deciding to unfold.
        await expect(page.getByTestId('board-row')).toBeAttached()
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
        await expectPinFits(pin, card, vp.width)
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
