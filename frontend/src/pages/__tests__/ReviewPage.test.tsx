import { act, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { vi } from 'vitest'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { queryKeys } from '../../hooks/queryKeys'
import { ReviewPage } from '../ReviewPage'

let req: object | null
let posts: unknown[]
const request = (revision: string, text = '<p>page</p>', answers: object = {}) => ({
  kind: 'plan-approval',
  content: { kind: 'readable', path: '.agent/review.html', media_type: 'text/html', text },
  revision,
  answers,
})

beforeEach(() => {
  posts = []
  req = request('r1', '<p>page</p>', { format: 'b' })
  server.use(
    http.get('/api/task/widget/42/request', () => HttpResponse.json(req)),
    http.post('/api/task/widget/42/answers', async ({ request: r }) => {
      posts.push(await r.json())
      return HttpResponse.json({ status: 'pending', intent: 'x' }, { status: 202 })
    }),
  )
})

async function renderReview() {
  const view = renderWithProviders(
    <Routes><Route path="/task/:target/:issue/review" element={<ReviewPage />} /></Routes>,
    { route: '/task/widget/42/review' },
  )
  const frame = await screen.findByTestId('review-frame') as HTMLIFrameElement
  const win = frame.contentWindow!
  const send = (data: object) => act(() => { window.dispatchEvent(new MessageEvent('message', { data, source: win })) })
  return { ...view, frame, win, send }
}

test('restores the saved answers after ready and posts a submission at once', async () => {
  const { frame, win, send } = await renderReview()
  expect(frame).toHaveAttribute('srcdoc', '<p>page</p>')
  const restore = vi.spyOn(win, 'postMessage')
  send({ type: 'ready', v: 1 })
  expect(restore).toHaveBeenCalledWith({ type: 'restore', v: 1, answers: { format: 'b' } }, '*')
  send({ type: 'answers', v: 1, answers: { format: 'a' }, submit: 'approve' })
  await waitFor(() => expect(posts).toEqual([{ answers: { format: 'a' }, submit: 'approve', revision: 'r1' }]))
})

test('pagehide flushes the draft, and a draft that arrives after it', async () => {
  const { send } = await renderReview()
  send({ type: 'answers', v: 1, answers: { format: 'a' }, submit: null })
  act(() => { window.dispatchEvent(new Event('pagehide')) })
  send({ type: 'answers', v: 1, answers: { format: 'b' }, submit: null })
  act(() => { window.dispatchEvent(new Event('pageshow')) })
  await waitFor(() => expect(posts).toHaveLength(2))
  expect(posts[1]).toEqual({ answers: { format: 'b' }, submit: null, revision: 'r1' })
})

test('a message with another v shows the newer-console notice', async () => {
  const { send } = await renderReview()
  send({ type: 'answers', v: 2, answers: {}, submit: null })
  expect(screen.getByTestId('review-notice')).toHaveTextContent('this review page needs a newer console')
})

test('a new revision reloads the page and shows the changed notice', async () => {
  const { frame, queryClient } = await renderReview()
  expect(screen.queryByTestId('review-notice')).not.toBeInTheDocument()
  req = request('r2', '<p>second</p>')
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.request('widget', 42) }))
  await waitFor(() => expect(frame).toHaveAttribute('srcdoc', '<p>second</p>'))
  expect(screen.getByTestId('review-notice')).toHaveTextContent('the plan changed; your selections were reset to the saved ones')
})

test('shows nothing while the request is not readable', async () => {
  req = { kind: 'plan-approval', content: { kind: 'unavailable', path: 'x', reason: 'gone' }, revision: 'r1', answers: {} }
  renderWithProviders(
    <Routes><Route path="/task/:target/:issue/review" element={<ReviewPage />} /></Routes>,
    { route: '/task/widget/42/review' },
  )
  await new Promise((r) => setTimeout(r, 50))
  expect(screen.queryByTestId('review-frame')).not.toBeInTheDocument()
})
