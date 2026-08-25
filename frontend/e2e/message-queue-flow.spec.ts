import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

// Scoped through the per-message row, never the bare chip testid: the thread
// renders one `message-state` per message, so an unqualified lookup only ever
// worked because this spec keeps exactly one message in flight.
const stateOf = (page: Page, id: string) =>
  page.getByTestId(`message-${id}`).getByTestId('message-state')

test('reply to a starved parked task: sending -> queued -> delivered', async ({ page, request }) => {
  await request.post('/__control__/reset-messages')
  await page.goto('/task/42')

  // The compose box states the contract before anything is sent.
  await expect(page.getByTestId('delivery-contract')).toHaveText(
    'will deliver when the session resumes — waiting for a free slot',
  )

  await page.getByLabel('Reply').fill('use the staging redirect URL')
  await page.getByRole('button', { name: 'Send reply & wake' }).click()

  // Written as an intent, not yet drained.
  await expect(page.getByTestId('message-thread')).toContainText(
    'use the staging redirect URL',
  )
  await expect(stateOf(page, 'intent-0')).toHaveText('sending')

  // The dispatcher pass drains the intent into the durable queue, but the
  // resume is still starved — the message must NOT be lost.
  await request.post('/__control__/apply-intents')
  await expect(stateOf(page, 'm1')).toHaveText('queued')
  await page.goto('/')
  await expect(
    page.getByTestId('card-42').getByTestId('mail-badge'),
  ).toHaveText('✉ 1')
  await expect(page.getByTestId('card-42')).toContainText(
    'waiting for a free slot',
  )

  // A slot frees: the task resumes and the message is delivered and stamped.
  await request.post('/__control__/free-slot')
  await page.goto('/task/42')
  await expect(stateOf(page, 'm1')).toContainText('delivered')
  await page.goto('/')
  await expect(page.getByTestId('card-42').getByTestId('mail-badge')).toBeHidden()
})
