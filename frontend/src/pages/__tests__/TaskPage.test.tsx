import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { pendingReplyIntent } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { TaskPage } from '../TaskPage'

function renderTask() {
  return renderWithProviders(
    <Routes>
      <Route path="/task/:issue" element={<TaskPage />} />
    </Routes>,
    { route: '/task/42' },
  )
}

beforeEach(() => server.use(...defaultHandlers))

it('renders card, pane tail, and worktree', async () => {
  renderTask()
  await waitFor(() =>
    expect(screen.getByText('Fix login redirect')).toBeInTheDocument(),
  )
  expect(screen.getByTestId('pane-tail').textContent).toContain(
    'staging redirect URL',
  )
  expect(screen.getByText('/home/agent/worktrees/task-42')).toBeInTheDocument()
})

it('reply POSTs the text and refetches pending intents — no optimistic state flip', async () => {
  let posted: unknown = null
  let intentCalls = 0
  server.use(
    http.post('/api/task/42/reply', async ({ request }) => {
      posted = await request.json()
      return HttpResponse.json(
        { status: 'pending', intent: '175-42-reply' }, { status: 202 },
      )
    }),
    http.get('/api/pending-intents', () => {
      intentCalls += 1
      return HttpResponse.json(
        intentCalls > 1 ? pendingReplyIntent : { intents: [] },
      )
    }),
  )
  renderTask()
  await waitFor(() =>
    expect(screen.getByText('Fix login redirect')).toBeInTheDocument(),
  )
  await userEvent.type(screen.getByLabelText('Reply'), 'use staging')
  await userEvent.click(screen.getByRole('button', { name: 'Send reply' }))
  await waitFor(() => expect(posted).toEqual({ text: 'use staging' }))
  // AWKWARD: pending badge appears; the card still reads parked (server truth)
  await waitFor(() =>
    expect(screen.getByTestId('pending-badge')).toBeInTheDocument(),
  )
  expect(screen.getByText(/parked: question/)).toBeInTheDocument()
})

it('unknown issue renders a not-found message', async () => {
  server.use(
    http.get('/api/task/:issue', () =>
      HttpResponse.json({ detail: 'unknown task' }, { status: 404 }),
    ),
  )
  renderTask()
  await waitFor(() =>
    expect(screen.getByText(/unknown task/)).toBeInTheDocument(),
  )
})
