import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { api, ApiError } from '../api'

it('GET /api/board returns typed BoardView', async () => {
  server.use(
    http.get('/api/board', () =>
      HttpResponse.json({
        columns: [{ key: 'parked', title: 'Parked', cards: [] }],
        capacity: { active: 1, capacity: 3, slots_used: 1, max_slots: 3 },
      }),
    ),
  )
  const board = await api.board()
  expect(board.capacity.max_slots).toBe(3)
  expect(board.columns[0]?.key).toBe('parked')
})

it('surfaces 422 detail as ApiError', async () => {
  server.use(
    http.post('/api/queue/next', () =>
      HttpResponse.json({ detail: 'issue 7 is blocked' }, { status: 422 }),
    ),
  )
  await expect(api.queueNext(7, true)).rejects.toThrow(ApiError)
  await expect(api.queueNext(7, true)).rejects.toThrow('issue 7 is blocked')
})

it('coerces an array-shaped 422 detail to a renderable string', async () => {
  // FastAPI returns `detail` as an array of validation-error objects for 422.
  // ApiError.detail is rendered directly as a React child, where an array of
  // objects throws "Objects are not valid as a React child".
  server.use(
    http.post('/api/queue/boost', () =>
      HttpResponse.json(
        {
          detail: [
            { loc: ['body', 'amount'], msg: 'Input should be a valid integer',
              type: 'int_parsing' },
            { loc: ['body', 'issue'], msg: 'Field required', type: 'missing' },
          ],
        },
        { status: 422 },
      ),
    ),
  )
  const err = await api.queueBoost(7, 1).catch((e: unknown) => e)
  expect(err).toBeInstanceOf(ApiError)
  expect(typeof (err as ApiError).detail).toBe('string')
  expect((err as ApiError).detail).toBe(
    'Input should be a valid integer; Field required',
  )
})

it('POST cancel is keyed by target and issue like every other intent', async () => {
  const bodies: unknown[] = []
  server.use(
    http.post('/api/task/portfolio_eval/42/cancel', async ({ request }) => {
      bodies.push(await request.json())
      return HttpResponse.json(
        { status: 'pending', intent: '1753444800000-portfolio_eval-42-cancel' },
        { status: 202 },
      )
    }),
  )
  await api.cancel('portfolio_eval', 42)
  expect(bodies).toEqual([{}])
})

it('POST intent action returns 202 pending', async () => {
  server.use(
    http.post('/api/task/widget/42/reply', () =>
      HttpResponse.json(
        { status: 'pending', intent: '1753444800000-42-reply' },
        { status: 202 },
      ),
    ),
  )
  const res = await api.reply('widget', 42, 'go ahead')
  expect(res.status).toBe('pending')
})

it('GET task detail hits /api/task/{target}/{issue}', async () => {
  let requestedPath = ''
  server.use(
    http.get('/api/task/:target/:issue', ({ params }) => {
      requestedPath = `/api/task/${params.target}/${params.issue}`
      return HttpResponse.json({
        card: { issue: 42, target: 'agent_ops', title: 't', stage: 's',
          park: '', park_note: '', column: 'c', slot: -1, branch: 'b',
          model: 'm', park_note_pending: false, feedback_pending: false,
          updated_at: '', consuming_capacity: false,
          claimed_at: '', cycle_seconds: null, score: null,
          undelivered_messages: 0, wake_blocked: false },
        pane_tail: '', session_alive: true, worktree: '', messages: [],
        delivery_contract: '', ci_run_id: 0, effort: null, labels: [],
        timeline: [],
      })
    }),
  )
  await api.taskDetail('agent_ops', 42)
  expect(requestedPath).toBe('/api/task/agent_ops/42')
})
