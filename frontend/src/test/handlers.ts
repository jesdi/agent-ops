import { http, HttpResponse } from 'msw'
import * as fx from './fixtures'

export const defaultHandlers = [
  http.get('/api/board/snapshot', () => HttpResponse.json({
    columns: fx.board.columns, capacity: fx.board.capacity,
    median_cycle_seconds: fx.board.median_cycle_seconds,
  })),
  http.get('/api/board', () => HttpResponse.json(fx.board)),
  http.get('/api/budget', () => HttpResponse.json(fx.budget)),
  http.get('/api/failures', () => HttpResponse.json(fx.failures)),
  http.get('/api/history', () => HttpResponse.json(fx.history)),
  http.get('/api/pending-intents', () => HttpResponse.json(fx.noPendingIntents)),
  http.get('/api/task/:target/:issue', () => HttpResponse.json(fx.taskDetail)),
  http.get('/api/task/:target/:issue/artifact', () =>
    HttpResponse.json({ detail: 'no artifact recorded' }, { status: 404 }),
  ),
  http.get('/api/task/:target/:issue/request', () =>
    HttpResponse.json(null),
  ),
]
