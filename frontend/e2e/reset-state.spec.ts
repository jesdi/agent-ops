import { spawn } from 'node:child_process'
import { readFileSync, readdirSync } from 'node:fs'
import { createServer } from 'node:net'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, request as http, test, type APIRequestContext } from '@playwright/test'

test('reset restores a task moved by free-slot to its seeded column', async ({ request }) => {
  // Establish the parked task independently of earlier files in this worker.
  await request.post('/__control__/reset')
  await request.post('/__control__/free-slot')
  const moved = await (await request.get('/api/board')).json()
  expect(moved.columns.find((c: { key: string }) => c.key === 'in-progress').cards)
    .toEqual(expect.arrayContaining([expect.objectContaining({ issue: 42 })]))

  const reset = await request.post('/__control__/reset')
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

// The contract cases below each start their own fake server, so "fresh" and
// "startup" mean a process nothing else has touched.
const FRONTEND = fileURLToPath(new URL('../', import.meta.url))
const ZERO_SLOTS = { active: 0, capacity: 3, slots_used: 0, max_slots: 5, slots_held: [] }

type Board = {
  columns: { key: string; title: string; zone: string; cards: { issue: number }[];
             ghosts: { number: number; boost: number }[] }[]
  capacity: unknown
  next_claim: Record<string, unknown>
}

async function freePort(): Promise<number> {
  const srv = createServer()
  await new Promise<void>((resolve) => srv.listen(0, '127.0.0.1', resolve))
  const { port } = srv.address() as { port: number }
  await new Promise((resolve) => srv.close(resolve))
  return port
}

async function freshServer() {
  const port = await freePort()
  const base = `http://127.0.0.1:${port}`
  const child = spawn(process.execPath, ['e2e/fake-api.mjs'], {
    cwd: FRONTEND, env: { ...process.env, FAKE_API_PORT: String(port) }, stdio: 'ignore',
  })
  const api = await http.newContext({ baseURL: base })
  for (let i = 0; ; i++) {
    try { if ((await api.get('/api/board')).ok()) break } catch { /* not up yet */ }
    if (i > 100) throw new Error(`fake api did not start on ${base}`)
    await new Promise((r) => setTimeout(r, 50))
  }
  return { api, base, stop: async () => { await api.dispose(); child.kill() } }
}

const contractTest = test.extend<{ fresh: Awaited<ReturnType<typeof freshServer>> }>({
  fresh: async ({}, use) => {
    const server = await freshServer()
    try {
      await use(server)
    } finally {
      await server.stop()
    }
  },
})

const get = async (api: APIRequestContext, path: string) => (await api.get(path)).json()
const post = (api: APIRequestContext, path: string, data?: unknown) => api.post(path, { data })
const withoutEta = (board: Board) => ({
  ...board, next_claim: { ...board.next_claim, next_pass_eta: undefined },
})
const column = (board: Board, key: string) => board.columns.find((c) => c.key === key)!
const allIssues = (board: Board) => board.columns.flatMap((c) => c.cards.map((x) => x.issue))

async function resetOk(api: APIRequestContext) {
  const res = await api.post('/__control__/reset')
  expect(res.status()).toBe(200)
  expect(await res.json()).toEqual({ ok: true })
}

async function expectCanonical(api: APIRequestContext, board0: Board, usage0: unknown) {
  expect(withoutEta(await get(api, '/api/board'))).toEqual(withoutEta(board0))
  expect(await get(api, '/api/usage')).toEqual(usage0)
}

// Reads `data:` frames from one live /api/events connection.
async function openEvents(base: string) {
  const ctl = new AbortController()
  const res = await fetch(`${base}/api/events`, { signal: ctl.signal })
  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const next = async (): Promise<string[]> => {
    for (;;) {
      const frame = buffer.indexOf('\n\n')
      if (frame >= 0) {
        const chunk = buffer.slice(0, frame)
        buffer = buffer.slice(frame + 2)
        const data = chunk.split('\n').find((l) => l.startsWith('data: '))
        if (data) return JSON.parse(data.slice(6)).changed
        continue
      }
      const read = await Promise.race([
        reader.read(),
        new Promise<never>((_, reject) =>
          setTimeout(() => reject(new Error('no SSE event within 3s')), 3000)),
      ])
      if (read.done) throw new Error('SSE connection closed')
      buffer += decoder.decode(read.value, { stream: true })
    }
  }
  return { next, close: () => ctl.abort() }
}

contractTest('mixed mutations: reset matches a fresh server apart from the ETA', async ({ fresh }) => {
  const { api } = fresh
  const board0: Board = await get(api, '/api/board')
  const usage0 = await get(api, '/api/usage')

  await post(api, '/__control__/seed-pinned')
  await post(api, '/api/queue/boost', { issue: 74, amount: 10 })
  await post(api, '/api/priority', { mode: 'openai' })
  await post(api, '/api/task/widget/42/reply', { text: 'applied reply' })
  await post(api, '/__control__/apply-intents')
  await post(api, '/__control__/free-slot')
  await post(api, '/api/task/widget/42/reply', { text: 'pending reply' })
  await resetOk(api)

  const board: Board = await get(api, '/api/board')
  const usage = await get(api, '/api/usage')
  expect(withoutEta(board)).toEqual(withoutEta(board0))
  expect(usage).toEqual(usage0)
  expect(allIssues(board)).not.toEqual(expect.arrayContaining([51]))
  expect(allIssues(board)).not.toEqual(expect.arrayContaining([52]))
  expect(usage.priority.pinned).toEqual([])
  expect(board.columns.map(({ key, title, zone }) => ({ key, title, zone })))
    .toEqual(board0.columns.map(({ key, title, zone }) => ({ key, title, zone })))
  expect(board0.capacity).toEqual(ZERO_SLOTS)
})

contractTest('message isolation: reset drops delivered, stored and pending messages', async ({ fresh }) => {
  const { api } = fresh
  const detail0 = await get(api, '/api/task/widget/42')

  await post(api, '/api/task/widget/42/reply', { text: 'delivered earlier' })
  await post(api, '/__control__/apply-intents')
  await post(api, '/__control__/free-slot')
  await post(api, '/api/task/widget/42/reply', { text: 'stored undelivered' })
  await post(api, '/__control__/apply-intents')
  await post(api, '/api/task/widget/42/reply', { text: 'still pending' })
  const before = await get(api, '/api/task/widget/42')
  expect(before.messages.map((m: { state: string }) => m.state))
    .toEqual(['delivered', 'queued', 'sending'])

  await resetOk(api)

  const detail = await get(api, '/api/task/widget/42')
  expect(detail.messages).toEqual([])
  expect(detail.delivery_contract).toBe(detail0.delivery_contract)
  expect(detail.card.undelivered_messages).toBe(0)
  expect(await get(api, '/api/pending-intents')).toEqual({ intents: [] })

  await post(api, '/__control__/apply-intents')
  expect((await get(api, '/api/task/widget/42')).messages).toEqual([])
  await post(api, '/api/task/widget/42/reply', { text: 'brand new' })
  await post(api, '/__control__/apply-intents')
  expect((await get(api, '/api/task/widget/42')).messages.map((m: { text: string }) => m.text))
    .toEqual(['brand new'])
})

contractTest('startup equivalence: startup state already equals the reset state', async ({ fresh }) => {
  const { api } = fresh
  const board0: Board = await get(api, '/api/board')
  const usage0 = await get(api, '/api/usage')
  expect(board0.capacity).toEqual(ZERO_SLOTS)

  await resetOk(api)
  await expectCanonical(api, board0, usage0)
})

contractTest('reset without mutations keeps canonical state and notifies each time', async ({ fresh }) => {
  const { api, base } = fresh
  const board0: Board = await get(api, '/api/board')
  const usage0 = await get(api, '/api/usage')
  const events = await openEvents(base)
  try {
    for (let i = 0; i < 2; i++) {
      await resetOk(api)
      expect(await events.next()).toEqual(['board', 'usage'])
      await expectCanonical(api, board0, usage0)
      expect((await get(api, '/api/task/widget/42')).messages).toEqual([])
      expect(await get(api, '/api/pending-intents')).toEqual({ intents: [] })
    }
  } finally {
    events.close()
  }
})

contractTest('repeated resets leave no duplicates, accumulated boosts or messages', async ({ fresh }) => {
  const { api } = fresh
  const board0: Board = await get(api, '/api/board')
  const usage0 = await get(api, '/api/usage')
  const mutate = async () => {
    await post(api, '/__control__/seed-pinned')
    await post(api, '/api/queue/boost', { issue: 74, amount: 10 })
    await post(api, '/api/task/widget/42/reply', { text: 'cycle reply' })
    await post(api, '/__control__/apply-intents')
    await post(api, '/__control__/free-slot')
  }

  await resetOk(api)
  await mutate()
  await resetOk(api)
  await resetOk(api)
  await expectCanonical(api, board0, usage0)
  await mutate()
  await resetOk(api)

  await expectCanonical(api, board0, usage0)
  const board: Board = await get(api, '/api/board')
  expect(allIssues(board)).toEqual([42])
  expect(column(board, 'queued').ghosts.map((g) => g.boost)).toEqual([0, 0])
  expect((await get(api, '/api/task/widget/42')).messages).toEqual([])
})

contractTest('queue and clock restoration: ghost order, next claim and a fresh ETA', async ({ fresh }) => {
  const { api } = fresh
  await post(api, '/api/queue/boost', { issue: 74, amount: 10 })
  const boosted: Board = await get(api, '/api/board')
  expect(column(boosted, 'queued').ghosts.map((g) => g.number)).toEqual([74, 73])
  const oldEta = boosted.next_claim.next_pass_eta
  await new Promise((r) => setTimeout(r, 20))

  const before = Date.now()
  await resetOk(api)
  const after = Date.now()

  const board: Board = await get(api, '/api/board')
  const ghosts = column(board, 'queued').ghosts
  expect(ghosts.map((g) => [g.number, g.boost])).toEqual([[73, 0], [74, 0]])
  const { next_pass_eta: eta, ...claim } = board.next_claim
  expect(claim).toEqual({
    verdict: 'will-claim', next_issue: 73, next_repository: 'widget',
    minutes_to_reset: 0, blocked_by: '',
  })
  const etaMs = Date.parse(eta as string)
  expect(etaMs).toBeGreaterThanOrEqual(before + 6 * 60_000)
  expect(etaMs).toBeLessThanOrEqual(after + 6 * 60_000)
  expect(eta).not.toBe(oldEta)
})

contractTest('connected consumer refresh: reset pushes board and usage on the live stream', async ({ fresh }) => {
  const { api, base } = fresh
  const board0: Board = await get(api, '/api/board')
  const usage0 = await get(api, '/api/usage')
  const events = await openEvents(base)
  try {
    await post(api, '/api/queue/boost', { issue: 74, amount: 10 })
    await post(api, '/api/priority', { mode: 'openai' })
    await post(api, '/__control__/seed-pinned')
    expect(await events.next()).toEqual(['board', 'queue'])
    expect(await events.next()).toEqual(['usage'])
    expect(await events.next()).toEqual(['board', 'usage'])

    await resetOk(api)
    expect(await events.next()).toEqual(['board', 'usage'])
    await expectCanonical(api, board0, usage0)

    await post(api, '/api/queue/boost', { issue: 74, amount: 1 })
    expect(await events.next()).toEqual(['board', 'queue'])
  } finally {
    events.close()
  }
})

test('migration: retired reset URLs are absent and the unified reset is documented', async () => {
  // Built from parts so this file does not itself match the retired URLs.
  const retired = new RegExp(`/__control__/reset-(${['queue', 'messages'].join('|')})`)
  const walk = (dir: string): string[] => readdirSync(dir, { withFileTypes: true })
    .filter((e) => !['node_modules', 'dist', 'test-results', 'playwright-report'].includes(e.name))
    .flatMap((e) => (e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)]))
  const offenders = walk(FRONTEND)
    .filter((f) => retired.test(readFileSync(f, 'utf8')))
    .map((f) => f.slice(FRONTEND.length))
  expect(offenders).toEqual([])

  const header = readFileSync(join(FRONTEND, 'e2e', 'fake-api.mjs'), 'utf8')
    .split('\n').filter((_, i, all) => all.slice(0, i + 1).every((x) => x.startsWith('//')))
    .join('\n')
  expect(header).toContain('POST /__control__/reset ')
})
