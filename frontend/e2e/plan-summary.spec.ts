import { expect, test } from '@playwright/test'

const SUMMARY = `# Plan review: Export a portfolio as CSV

## Tickets

1. **01 Export endpoint** — blocked by: none — seam: \`GET /api/export\`
2. **02 Owner check** — blocked by: 01 — seam: \`GET /api/export\`
3. **03 Column order** — blocked by: 01 — seam: \`PortfolioCsvWriter.write_rows_in_fixed_order\`
4. **04 Console button** — blocked by: 02, 03 — seam: \`ExportButton\`

## Open questions

- Should the export hold closed positions? Recommendation: no, the issue names open positions only.

## Corrections

None.
`

test('the plan summary reads as a document at 390px', async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 1000 })
  await request.post('/__control__/reset-messages')
  await page.route('**/api/task/widget/42/request', route => route.fulfill({ json: {
    kind: 'plan-approval',
    content: { kind: 'readable', path: '.agent/plan-review.md', media_type: 'text/markdown', text: SUMMARY },
  } }))
  await page.goto('/task/widget/42')
  const panel = page.getByTestId('request-panel')
  const tickets = panel.getByRole('heading', { name: 'Tickets', level: 2 })
  await expect(tickets).toBeVisible()
  // Section headings stand out from the body text, and list items carry markers.
  const body = panel.getByText('None.', { exact: true })
  const weight = (el: Element) => Number(getComputedStyle(el).fontWeight)
  const size = (el: Element) => parseFloat(getComputedStyle(el).fontSize)
  expect(await tickets.evaluate(weight)).toBeGreaterThanOrEqual(600)
  expect(await tickets.evaluate(size)).toBeGreaterThan(await body.evaluate(size))
  expect(await panel.locator('ol').evaluate(el => getComputedStyle(el).listStyleType)).toBe('decimal')
  expect(await panel.locator('ul').evaluate(el => getComputedStyle(el).listStyleType)).toBe('disc')
  // A 40-character seam name wraps inside the panel.
  await expect(panel.getByText('PortfolioCsvWriter.write_rows_in_fixed_order')).toBeVisible()
  expect(await panel.locator('.markdown').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
  expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
  await panel.screenshot({ path: '/tmp/plan-summary-390.png' })
})
