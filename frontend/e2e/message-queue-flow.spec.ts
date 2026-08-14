import { expect, test } from '@playwright/test'

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
  await expect(page.getByTestId('message-state')).toHaveText('sending')

  // The dispatcher pass drains the intent into the durable queue, but the
  // resume is still starved — the message must NOT be lost.
  await request.post('/__control__/apply-intents')
  await expect(page.getByTestId('message-state')).toHaveText('queued')
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
  await expect(page.getByTestId('message-state')).toContainText('delivered')
  await page.goto('/')
  await expect(page.getByTestId('card-42').getByTestId('mail-badge')).toBeHidden()
})
