/// <reference lib="dom" />
import { expect, test } from '@playwright/test'
import { showHeader } from './phone.js'

test.beforeEach(async ({ request }) => {
  await request.post('/__control__/reset-queue')
})

const control = (page: import('@playwright/test').Page) => page.getByRole('radiogroup', { name: /priority/i })

test('choosing OpenAI sticks across a reload and puts the first chip on openai', async ({ page }) => {
  await page.goto('/')
  await showHeader(page)
  await expect(control(page).getByRole('radio', { name: 'Auto' })).toBeChecked()
  await control(page).getByRole('radio', { name: 'OpenAI' }).click()
  await expect(control(page).getByRole('radio', { name: 'OpenAI' })).toBeChecked()

  await page.reload()
  await showHeader(page)
  await expect(control(page).getByRole('radio', { name: 'OpenAI' })).toBeChecked()
  const chip = page.getByText('first', { exact: true })
  await expect(chip).toHaveCount(1)
  // The innermost block holding the chip and the openai heading, not anthropic's.
  const column = page.locator('div').filter({ has: chip }).filter({ hasText: /openai/i })
    .filter({ hasNotText: /anthropic/i })
  await expect(column.last()).toBeVisible()
})

for (const colorScheme of ['light', 'dark'] as const) {
  test(`at 390px the priority control fits, no horizontal scroll (${colorScheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/')
    // 390px folds the header on every project, so showHeader's project check
    // does not apply here.
    await page.getByRole('button', { name: /active/, expanded: false }).click()
    const box = (await control(page).boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(390)
    expect(await page.evaluate<boolean>('document.documentElement.scrollWidth <= document.documentElement.clientWidth')).toBe(true)
  })
}

type Box = { x: number; y: number; width: number; height: number }
const overlap = (a: Box, b: Box) =>
  a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height

async function phone(page: import('@playwright/test').Page) {
  await page.emulateMedia({ colorScheme: 'light' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: /active/, expanded: false }).click()
}

test('at 390px every radio is a 44px tap target and none overlap', async ({ page }) => {
  await phone(page)
  const radios = control(page).getByRole('radio')
  const boxes: Box[] = []
  for (let i = 0; i < (await radios.count()); i++) boxes.push((await radios.nth(i).boundingBox())!)
  expect(boxes.length).toBeGreaterThanOrEqual(3)
  for (const b of boxes) {
    expect(b.height).toBeGreaterThanOrEqual(44)
    expect(b.width).toBeGreaterThanOrEqual(44)
  }
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) expect(overlap(boxes[i]!, boxes[j]!)).toBe(false)
})

test('at 390px with openai chosen the chip and pace texts fit and do not collide', async ({ page }) => {
  await phone(page)
  await control(page).getByRole('radio', { name: 'OpenAI' }).click()
  await expect(control(page).getByRole('radio', { name: 'OpenAI' })).toBeChecked()
  const chip = page.getByText('first', { exact: true })
  await expect(chip).toHaveCount(1)
  const paces = page.getByText(/× pace$/)
  expect(await paces.count()).toBe(2)
  // The heading span holds the provider name as a bare text node before the chip.
  const heading = await chip.evaluate((el) => {
    const text = [...el.parentElement!.childNodes].find((n) => n.nodeType === Node.TEXT_NODE)!
    const r = document.createRange()
    r.selectNodeContents(text)
    const { x, y, width, height } = r.getBoundingClientRect()
    return { x, y, width, height, text: text.textContent ?? '' }
  })
  expect(heading.text.trim()).toBe('openai')
  const targets: Box[] = [(await chip.boundingBox())!]
  for (let i = 0; i < 2; i++) targets.push((await paces.nth(i).boundingBox())!)
  for (const b of targets) {
    expect(b.x).toBeGreaterThanOrEqual(0)
    expect(b.x + b.width).toBeLessThanOrEqual(390)
  }
  const [c, ...ps] = targets
  expect(overlap(c!, heading)).toBe(false)
  for (const p of ps) expect(overlap(c!, p)).toBe(false)
})
