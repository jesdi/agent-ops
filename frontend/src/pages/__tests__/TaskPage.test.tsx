import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { Link, Route, Routes } from 'react-router'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { pendingReplyIntent, taskDetail } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { useUiStore } from '../../store/ui'
import { TaskPage } from '../TaskPage'

// Real xterm + a real WebSocket in jsdom would be noise here; these tests
// only ever ask WHETHER the terminal attached, never what it renders.
vi.mock('../../components/Terminal', () => ({
  Terminal: ({ issue }: { issue: number }) => (
    <div data-testid="terminal" data-issue={issue} />
  ),
}))

function renderTask(route = '/task/42') {
  return renderWithProviders(
    <>
      <Link to="/task/43">go to 43</Link>
      <Routes>
        <Route path="/task/:issue" element={<TaskPage />} />
      </Routes>
    </>,
    { route },
  )
}

beforeEach(() => {
  server.use(...defaultHandlers)
  useUiStore.setState({ terminalOpenFor: null })
})

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

it('a failing intent surfaces the API detail inline instead of looking like success', async () => {
  server.use(
    http.post('/api/task/42/kill', () =>
      HttpResponse.json({ detail: 'no task 42' }, { status: 404 }),
    ),
  )
  renderTask()
  await waitFor(() =>
    expect(screen.getByText('Fix login redirect')).toBeInTheDocument(),
  )
  await userEvent.click(screen.getByRole('button', { name: 'Kill' }))
  await userEvent.click(screen.getByRole('button', { name: 'Confirm kill?' }))
  await waitFor(() =>
    expect(screen.getByTestId('action-error')).toHaveTextContent('no task 42'),
  )
  // no badge either — the intent was never written
  expect(screen.queryByTestId('pending-badge')).not.toBeInTheDocument()
})

it('a 422 with an array-shaped detail renders readable text, not [object Object]', async () => {
  server.use(
    http.post('/api/task/42/park', () =>
      HttpResponse.json(
        { detail: [{ loc: ['body', 'issue'], msg: 'field required', type: 'missing' }] },
        { status: 422 },
      ),
    ),
  )
  renderTask()
  await waitFor(() =>
    expect(screen.getByText('Fix login redirect')).toBeInTheDocument(),
  )
  await userEvent.click(screen.getByRole('button', { name: 'Park now' }))
  await waitFor(() =>
    expect(screen.getByTestId('action-error')).toHaveTextContent('field required'),
  )
  expect(screen.getByTestId('action-error').textContent).not.toContain(
    '[object Object]',
  )
})

it('a later success clears the inline error', async () => {
  let first = true
  server.use(
    http.post('/api/task/42/park', () => {
      if (first) {
        first = false
        return HttpResponse.json({ detail: 'no task 42' }, { status: 404 })
      }
      return HttpResponse.json(
        { status: 'pending', intent: '175-42-park' }, { status: 202 },
      )
    }),
  )
  renderTask()
  await waitFor(() =>
    expect(screen.getByText('Fix login redirect')).toBeInTheDocument(),
  )
  await userEvent.click(screen.getByRole('button', { name: 'Park now' }))
  await waitFor(() => expect(screen.getByTestId('action-error')).toBeInTheDocument())
  await userEvent.click(screen.getByRole('button', { name: 'Park now' }))
  await waitFor(() =>
    expect(screen.queryByTestId('action-error')).not.toBeInTheDocument(),
  )
})

it('an attached terminal does not carry over to the next task navigated to', async () => {
  renderTask()
  await waitFor(() =>
    expect(screen.getByText('Fix login redirect')).toBeInTheDocument(),
  )
  await userEvent.click(screen.getByRole('button', { name: 'Attach terminal' }))
  expect(screen.getByTestId('terminal')).toHaveAttribute('data-issue', '42')

  // Navigating to another task must NOT auto-attach it: an attach writes the
  // `attached-<N>` marker and the dispatcher then declines to drive the task.
  await userEvent.click(screen.getByRole('link', { name: 'go to 43' }))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Attach terminal' })).toBeInTheDocument(),
  )
  expect(screen.queryByTestId('terminal')).not.toBeInTheDocument()
  expect(screen.getByTestId('pane-tail')).toBeInTheDocument()
})

it('a dead session explains itself on load and disables the attach button', async () => {
  server.use(
    http.get('/api/task/:issue', () =>
      HttpResponse.json({ ...taskDetail, session_alive: false }),
    ),
  )
  renderTask()
  await waitFor(() =>
    expect(screen.getByText('session task-42 is not running')).toBeInTheDocument(),
  )
  expect(screen.getByRole('button', { name: 'Attach terminal' })).toBeDisabled()
  expect(screen.getByTestId('pane-tail')).toBeInTheDocument()
})

it('a non-numeric issue param renders not-found instead of requesting /api/task/NaN', async () => {
  renderWithProviders(
    <Routes>
      <Route path="/task/:issue" element={<TaskPage />} />
    </Routes>,
    { route: '/task/abc' },
  )
  expect(await screen.findByText(/is not a task number/)).toBeInTheDocument()
})

it('park does not wipe typed reply text', async () => {
  server.use(
    http.post('/api/task/42/park', () =>
      HttpResponse.json({ status: 'pending', intent: '175-42-park' }, { status: 202 }),
    ),
  )
  renderTask()
  await waitFor(() =>
    expect(screen.getByText('Fix login redirect')).toBeInTheDocument(),
  )
  await userEvent.type(screen.getByLabelText('Reply'), 'half-written thought')
  await userEvent.click(screen.getByRole('button', { name: 'Park now' }))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Park now' })).toBeEnabled(),
  )
  expect(screen.getByLabelText('Reply')).toHaveValue('half-written thought')
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
