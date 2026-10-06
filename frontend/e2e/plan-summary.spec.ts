import { expect, test } from '@playwright/test'

const SEAM = 'PortfolioCsvWriter.write_rows_in_fixed_order'
const SUMMARY = `# Plan review: Export a portfolio as CSV

## Tickets

1. **01 Export endpoint** — blocked by: none — seam: \`GET /api/export\`
2. **02 Owner check** — blocked by: 01 — seam: \`GET /api/export\`
3. **03 Column order** — blocked by: 01 — seam: \`${SEAM}\`
4. **04 Console button** — blocked by: 02, 03 — seam: \`ExportButton\`
5. **05 Empty portfolio** — blocked by: 01 — seam: \`GET /api/export\`

## Open questions

### Closed positions

- Should the export hold closed positions? Recommendation: no, the issue names open positions only.

## Corrections

- Requirement 3 named a currency column with no source in the issue. It is removed. Before:

\`\`\`
3. The file has the columns symbol, quantity, price, currency, market value and unrealised gain in this order.
\`\`\`
`

for (const scheme of ['light', 'dark'] as const) {
  test(`the plan summary reads as a document at 390px (${scheme})`, async ({ page, request }) => {
    await page.setViewportSize({ width: 390, height: 1000 })
    await page.emulateMedia({ colorScheme: scheme })
    await request.post('/__control__/reset-messages')
    await page.route('**/api/task/widget/42/request', route => route.fulfill({ json: {
      kind: 'plan-approval',
      content: { kind: 'readable', path: '.agent/plan-review.md', media_type: 'text/markdown', text: SUMMARY },
    } }))
    await page.goto('/task/widget/42')
    const panel = page.getByTestId('request-panel')
    const doc = panel.locator('.markdown')
    const h2 = panel.getByRole('heading', { name: 'Tickets', level: 2 })
    const h3 = panel.getByRole('heading', { name: 'Closed positions', level: 3 })
    await expect(h2).toBeVisible()
    // Section headings stand out from the body text, and list items carry markers.
    const body = panel.getByText(/Should the export hold closed positions/)
    const weight = (el: Element) => Number(getComputedStyle(el).fontWeight)
    const size = (el: Element) => parseFloat(getComputedStyle(el).fontSize)
    expect(await h2.evaluate(weight)).toBeGreaterThanOrEqual(600)
    expect(await h3.evaluate(weight)).toBeGreaterThanOrEqual(600)
    // h3 is one step between h2 and the body text.
    expect(await h2.evaluate(size)).toBeGreaterThan(await h3.evaluate(size))
    expect(await h3.evaluate(size)).toBeGreaterThan(await body.evaluate(size))
    expect(await panel.locator('ol').evaluate(el => getComputedStyle(el).listStyleType)).toBe('decimal')
    expect(await panel.locator('ul').first().evaluate(el => getComputedStyle(el).listStyleType)).toBe('disc')
    // A 44-character seam name wraps inside the panel, at a separator.
    const seam = panel.locator('code', { hasText: SEAM })
    await expect(seam).toBeVisible()
    const lines = await seam.evaluate(el => [...el.getClientRects()].length)
    expect(lines).toBeGreaterThan(1)
    expect(await seam.evaluate(el => {
      const range = document.createRange()
      const last = [...el.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).pop()!
      range.selectNodeContents(last)
      const top = (r: DOMRect) => Math.round(r.top)
      const rects = [...range.getClientRects()]
      return rects.every(r => top(r) === top(rects[0]))
    })).toBe(true)   // the last piece (`order`) is whole on its line: no orphan letter
    // A fenced block is a visible box, and its long line scrolls inside it.
    const pre = panel.locator('pre')
    expect(await pre.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe('rgba(0, 0, 0, 0)')
    expect(await pre.evaluate(el => parseFloat(getComputedStyle(el).paddingLeft))).toBeGreaterThan(0)
    expect(await pre.evaluate(el => getComputedStyle(el).overflowX)).toBe('auto')
    expect(await pre.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true)
    expect(await pre.evaluate(el => el.getBoundingClientRect().height)).toBeLessThan(50)   // one line, not broken
    // The summary is the review document: it flows with the page, no scroll box.
    expect(await doc.evaluate(el => getComputedStyle(el).maxHeight)).toBe('none')
    expect(await doc.evaluate(el => el.scrollHeight <= el.clientHeight && el.scrollWidth <= el.clientWidth)).toBe(true)
    expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(390)
    await panel.screenshot({ path: `/tmp/plan-summary-390-${scheme}.png` })
  })
}
