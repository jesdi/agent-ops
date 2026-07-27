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
