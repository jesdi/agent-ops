// Seeded fake backend for Playwright E2E. Serves frontend/dist with SPA
// fallback, the /api contract from mutable seed state, SSE, and the
// terminal WebSocket. POST /__control__/apply-intents plays the
// dispatcher: clears intents, moves the parked card to in-progress, and
// pushes an SSE board-changed event.
import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { extname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { WebSocketServer } from 'ws'

const DIST = fileURLToPath(new URL('../dist/', import.meta.url))
const PORT = 8481

const parkedCard = {
  issue: 42, target: 'jesdi/widget', title: 'Fix login redirect',
  stage: 'implement', park: 'question', column: 'parked', slot: 1,
  branch: 'fix/login-redirect', model: 'sonnet', park_note_pending: true,
  updated_at: '2026-07-25T10:00:00Z', attached: false,
  // question-parked: container stopped, unit released
  consuming_capacity: false,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null,
}

const state = {
  board: {
    columns: [
      { key: 'queued', title: 'Queued', cards: [] },
      { key: 'in-progress', title: 'In progress', cards: [] },
      { key: 'needs-review', title: 'Needs review', cards: [] },
      { key: 'parked', title: 'Parked', cards: [parkedCard] },
      { key: 'awaiting-ci', title: 'Awaiting CI', cards: [] },
      { key: 'resuming', title: 'Resuming', cards: [] },
      { key: 'stalled', title: 'Stalled', cards: [] },
      { key: 'failed', title: 'Failed', cards: [] },
      { key: 'pr-open', title: 'PR open', cards: [] },
    ],
    capacity: { active: 0, capacity: 3, slots_used: 1, max_slots: 3 },
    upcoming: [
      { number: 73, target: 'jesdi/widget', title: 'Ship dark mode',
        url: 'https://github.com/jesdi/widget/issues/73', score: 8.5, boost: 0,
        quarantined: false },
      { number: 74, target: 'jesdi/widget', title: 'Fix flaky test',
        url: 'https://github.com/jesdi/widget/issues/74', score: 3.5, boost: 0,
        quarantined: false },
    ],
    upcoming_stale: false,
    median_cycle_seconds: 7200,
    next_claim: {
      verdict: 'will-claim',
      next_pass_eta: new Date(Date.now() + 6 * 60_000).toISOString(),
      next_issue: 73, next_target: 'jesdi/widget', minutes_to_reset: 0,
    },
  },
  queue: { targets: [] },
  budget: {
    utilization: 0.4, minutes_to_reset: 120, source: 'oauth',
    would_spawn: true, threshold_applied: 'base',
  },
  failures: { quarantined: [], fingerprints: [] },
  history: { events: [] },
  intents: [],
}

function taskDetail(issue) {
  const card = state.board.columns
    .flatMap((c) => c.cards)
    .find((c) => c.issue === issue)
  if (!card) return null
  return {
    card,
    pane_tail: '? Should I use the staging redirect URL or prod?\n> ',
    session_alive: true,
    worktree: `/home/agent/worktrees/task-${issue}`,
    pending_reply: '', ci_run_id: 0, effort: 3, labels: ['auto'],
    timeline: [],
  }
}

const sseClients = new Set()
const push = (changed) => {
  for (const res of sseClients) {
    res.write(`data: ${JSON.stringify({ changed })}\n\n`)
  }
}

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`)
  const json = (code, body) => {
    res.writeHead(code, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(body))
  }

  if (url.pathname === '/api/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    })
    res.write(':\n\n')
    sseClients.add(res)
    req.on('close', () => sseClients.delete(res))
    return
  }
  if (url.pathname === '/api/board') return json(200, state.board)
  if (url.pathname === '/api/queue') return json(200, state.queue)
  if (url.pathname === '/api/budget') return json(200, state.budget)
  if (url.pathname === '/api/failures') return json(200, state.failures)
  if (url.pathname === '/api/history') return json(200, state.history)
  if (url.pathname === '/api/pending-intents') {
    return json(200, { intents: state.intents })
  }
  const detailMatch = url.pathname.match(/^\/api\/task\/(\d+)$/)
  if (detailMatch && req.method === 'GET') {
    const detail = taskDetail(Number(detailMatch[1]))
    return detail ? json(200, detail) : json(404, { detail: 'unknown task' })
  }
  const descMatch = url.pathname.match(/^\/api\/task\/(\d+)\/description$/)
  if (descMatch && req.method === 'GET') {
    const n = Number(descMatch[1])
    // Mirror real backend precedence: claimed task first, then queue ghost,
    // then 404 — identical to web/app.py task_description().
    const claimedCard = state.board.columns
      .flatMap((c) => c.cards)
      .find((c) => c.issue === n)
    if (claimedCard) {
      return json(200, {
        title: claimedCard.title,
        body: `## Goal\nBody of issue ${n}.`,
        url: `https://github.com/${claimedCard.target}/issues/${n}`,
        fetched_at: new Date().toISOString(), error: '',
      })
    }
    const ghost = state.board.upcoming.find((g) => g.number === n)
    if (ghost) {
      return json(200, {
        title: ghost.title, body: `## Goal\nBody of issue ${n}.`,
        url: ghost.url, fetched_at: new Date().toISOString(), error: '',
      })
    }
    return json(404, { detail: `issue ${n} is neither a task nor on any queue` })
  }
  const intentMatch = url.pathname.match(
    /^\/api\/task\/(\d+)\/(reply|park|kill|retry|resume)$/,
  )
  if (intentMatch && req.method === 'POST') {
    const issue = Number(intentMatch[1])
    const action = intentMatch[2]
    state.intents.push({
      action, issue, actor: 'dev@localhost',
      created_at: new Date().toISOString(),
    })
    return json(202, { status: 'pending', intent: `${Date.now()}-${issue}-${action}` })
  }
  if (url.pathname === '/api/queue/boost' && req.method === 'POST') {
    let body = ''
    req.on('data', (c) => { body += c })
    req.on('end', () => {
      let parsed
      try { parsed = JSON.parse(body) } catch {
        json(400, { detail: 'invalid json body' }); return
      }
      const { issue, amount } = parsed
      const g = state.board.upcoming.find((x) => x.number === issue)
      if (g) {
        g.boost += amount
        state.board.upcoming.sort((a, b) => b.boost - a.boost || b.score - a.score)
        state.board.next_claim = { ...state.board.next_claim, next_issue: state.board.upcoming[0].number }
      }
      push(['board', 'queue'])
      json(200, { ok: true, reason: 'boosted' })
    })
    return
  }
  if (url.pathname === '/api/queue/next' && req.method === 'POST') {
    return json(200, { ok: true, reason: 'queued next' })
  }
  if (url.pathname === '/api/queue/ready' && req.method === 'POST') {
    return json(200, { ok: true, reason: 'marked ready' })
  }
  if (url.pathname.startsWith('/api/')) {
    return json(404, { detail: 'not found' })
  }
  // Resets only the mutable queue state touched by queue-flow.spec.ts so the
  // spec is idempotent across Playwright retries (workers:1, no parallel runs).
  if (url.pathname === '/__control__/reset-queue' && req.method === 'POST') {
    state.board.upcoming = [
      { number: 73, target: 'jesdi/widget', title: 'Ship dark mode',
        url: 'https://github.com/jesdi/widget/issues/73', score: 8.5, boost: 0,
        quarantined: false },
      { number: 74, target: 'jesdi/widget', title: 'Fix flaky test',
        url: 'https://github.com/jesdi/widget/issues/74', score: 3.5, boost: 0,
        quarantined: false },
    ]
    state.board.next_claim = {
      verdict: 'will-claim',
      next_pass_eta: new Date(Date.now() + 6 * 60_000).toISOString(),
      next_issue: 73, next_target: 'jesdi/widget', minutes_to_reset: 0,
    }
    return json(200, { ok: true })
  }
  if (url.pathname === '/__control__/apply-intents' && req.method === 'POST') {
    state.intents = []
    const parked = state.board.columns.find((c) => c.key === 'parked')
    const inProgress = state.board.columns.find((c) => c.key === 'in-progress')
    inProgress.cards.push(
      ...parked.cards.map((c) => ({
        ...c, column: 'in-progress', park: '', consuming_capacity: true,
      })),
    )
    parked.cards = []
    state.board.capacity = { ...state.board.capacity, active: inProgress.cards.length }
    push(['board', 'history'])
    return json(200, { ok: true })
  }

  // Static dist with SPA fallback (mirrors the plan-2 backend).
  let file = join(DIST, url.pathname === '/' ? 'index.html' : url.pathname.slice(1))
  if (!existsSync(file)) file = join(DIST, 'index.html')
  res.writeHead(200, {
    'Content-Type': MIME[extname(file)] ?? 'application/octet-stream',
  })
  res.end(await readFile(file))
})

const wss = new WebSocketServer({ noServer: true })
server.on('upgrade', (req, socket, head) => {
  const match = req.url?.match(/^\/api\/task\/(\d+)\/terminal$/)
  if (!match) return socket.destroy()
  wss.handleUpgrade(req, socket, head, (ws) => {
    const issue = match[1]
    ws.send(Buffer.from(`agent-ops $ hello from task ${issue}\r\n`))
    ws.on('message', (data, isBinary) => {
      if (isBinary) ws.send(data) // echo terminal bytes
    })
  })
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`fake api serving ${DIST} on http://127.0.0.1:${PORT}`)
})
