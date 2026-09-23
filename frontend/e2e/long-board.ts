import type { Page } from '@playwright/test'

/** Serves the fake board with every column but Wont do occupied and a
 *  40-ghost Queued column: tall enough to overflow any column and wide
 *  enough to overflow the row at desktop widths. */
export async function routeLongBoard(page: Page) {
  await page.route('**/api/board', async (route) => {
    const response = await route.fetch()
    const board = await response.json()
    const seed = board.columns.find((c: { key: string }) => c.key === 'parked').cards[0]
    for (const [i, column] of board.columns.entries()) {
      if (column.key === 'wont-do' || column.key === 'queued') continue
      for (let n = 0; n < 3 + (i % 3) * 4; n++) {
        const issue = 100 + i * 20 + n
        column.cards.push({
          ...seed, issue, column: column.key, park_note_pending: false, wake_blocked: false,
          park: column.key === 'parked' ? 'question' : '', stage: 'implement',
          title: `Long-board card ${issue} with a title that wraps onto a second line`,
        })
      }
    }
    for (let n = 0; n < 40; n++) {
      board.upcoming.push({
        number: 200 + n, target: 'widget', title: `Queued idea ${200 + n}`,
        url: `https://github.com/jesdi/widget/issues/${200 + n}`, score: 3 - n / 100, boost: 0,
      })
    }
    await route.fulfill({ response, json: board })
  })
}
