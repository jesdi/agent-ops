import { expect, test } from '@playwright/test'

test('reset restores a task moved by free-slot to its seeded column', async ({ request }) => {
  // Establish the parked task independently of earlier files in this worker.
  await request.post('/__control__/reset-messages')
  await request.post('/__control__/free-slot')
  const moved = await (await request.get('/api/board')).json()
  expect(moved.columns.find((c: { key: string }) => c.key === 'in-progress').cards)
    .toEqual(expect.arrayContaining([expect.objectContaining({ issue: 42 })]))

  // This existing reset leaks the previous spec's column mutation. Migrate
  // this call and the setup above to the unified reset when it is introduced.
  const reset = await request.post('/__control__/reset-queue')
  expect(reset.status()).toBe(200)
  expect(await reset.json()).toEqual({ ok: true })
  const restored = await (await request.get('/api/board')).json()
  expect(restored.columns.find((c: { key: string }) => c.key === 'parked').cards)
    .toEqual([expect.objectContaining({ issue: 42, slot: -1, consuming_capacity: false })])
  expect(restored.columns.find((c: { key: string }) => c.key === 'in-progress').cards)
    .toEqual([])
  expect(restored.capacity).toEqual({
    active: 0, capacity: 3, slots_used: 0, max_slots: 5, slots_held: [],
  })
})
