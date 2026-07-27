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

it('POST intent action returns 202 pending', async () => {
  server.use(
    http.post('/api/task/42/reply', () =>
      HttpResponse.json(
        { status: 'pending', intent: '1753444800000-42-reply' },
        { status: 202 },
      ),
    ),
  )
  const res = await api.reply(42, 'go ahead')
  expect(res.status).toBe('pending')
})
