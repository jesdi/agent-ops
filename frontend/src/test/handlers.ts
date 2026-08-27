import { http, HttpResponse } from 'msw'
import * as fx from './fixtures'

export const defaultHandlers = [
  http.get('/api/board', () => HttpResponse.json(fx.board)),
  http.get('/api/budget', () => HttpResponse.json(fx.budget)),
  http.get('/api/failures', () => HttpResponse.json(fx.failures)),
  http.get('/api/history', () => HttpResponse.json(fx.history)),
  http.get('/api/pending-intents', () => HttpResponse.json(fx.noPendingIntents)),
  http.get('/api/task/:target/:issue', () => HttpResponse.json(fx.taskDetail)),
]
