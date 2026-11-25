import { readFileSync } from 'node:fs'
import type { QueryClient } from '@tanstack/react-query'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { vi } from 'vitest'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { queryKeys } from '../../hooks/queryKeys'
import { ReviewPage } from '../ReviewPage'

let req: object | null
let delivery: object | null
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
  delivery = null
  server.use(
    http.get('/api/task/widget/42/request', () => HttpResponse.json(req)),
    http.get('/api/task/widget/42/delivery', () => HttpResponse.json(delivery)),
    http.post('/api/task/widget/42/answers', async ({ request: r }) => {
      posts.push(await r.json())
      return HttpResponse.json({ status: 'pending', intent: 'x' }, { status: 202 })
    }),
  )
})

async function renderReview() {
  const view = renderWithProviders(
    <Routes><Route path="/task/:repository/:issue/review" element={<ReviewPage />} /></Routes>,
    { route: '/task/widget/42/review' },
  )
  const frame = await screen.findByTestId('review-frame') as HTMLIFrameElement
  const win = frame.contentWindow!
  const send = (data: object) => act(() => { window.dispatchEvent(new MessageEvent('message', { data, source: win })) })
  return { ...view, frame, win, send }
}

test('restores the saved answers after ready; Approve needs a second tap and posts the page\'s last set', async () => {
  const { frame, win, send } = await renderReview()
  expect(frame).toHaveAttribute('srcdoc', '<p>page</p>')
  const restore = vi.spyOn(win, 'postMessage')
  send({ type: 'ready', v: 1 })
  expect(restore).toHaveBeenCalledWith({ type: 'restore', v: 1, answers: { format: 'b' } }, '*')
  send({ type: 'answers', v: 1, answers: { format: 'a' }, submit: null })
  fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
  expect(posts).toEqual([])
  fireEvent.click(screen.getByRole('button', { name: 'Tap again to approve' }))
  await waitFor(() => expect(posts).toEqual([{ answers: { format: 'a' }, submit: 'approve', revision: 'r1' }]))
  expect(screen.getByTestId('review-notice')).toHaveTextContent('approved; implement starts with these answers')
  expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
})

test('a page that posts submit approve approves nothing: it is saved as a draft', async () => {
  const { send } = await renderReview()
  send({ type: 'answers', v: 1, answers: { format: 'a' }, submit: 'approve' })
  act(() => { window.dispatchEvent(new Event('pagehide')) })
  await waitFor(() => expect(posts).toEqual([{ answers: { format: 'a' }, submit: null, revision: 'r1' }]))
})

test('Send changes posts at once; a questionnaire has one button, Send answers', async () => {
  const first = await renderReview()
  fireEvent.click(screen.getByRole('button', { name: 'Send changes' }))
  await waitFor(() => expect(posts).toEqual([{ answers: {}, submit: 'changes', revision: 'r1' }]))
  first.unmount()
  req = { ...request('r1'), kind: 'answers' }
  await renderReview()
  expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual(['Send answers'])
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
  const { queryClient } = await renderReview()
  expect(screen.queryByTestId('review-notice')).not.toBeInTheDocument()
  req = request('r2', '<p>second</p>')
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.request('widget', 42) }))
  await waitFor(() => expect(screen.getByTestId('review-frame')).toHaveAttribute('srcdoc', '<p>second</p>'))
  expect(screen.getByTestId('review-notice')).toHaveTextContent('the plan changed; your selections were reset to the saved ones')
})

test('the changed notice goes after 6 s, and at once on tap', async () => {
  const { queryClient } = await renderReview()
  vi.useFakeTimers({ shouldAdvanceTime: true })
  try {
    const change = async (revision: string) => {
      req = request(revision, `<p>${revision}</p>`)
      await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.request('widget', 42) }))
      await waitFor(() => expect(screen.getByTestId('review-notice')).toBeInTheDocument())
    }
    await change('r2')
    act(() => { vi.advanceTimersByTime(5900) })
    expect(screen.getByTestId('review-notice')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(200) })
    expect(screen.queryByTestId('review-notice')).not.toBeInTheDocument()
    await change('r3')
    act(() => { screen.getByTestId('review-notice').click() })
    expect(screen.queryByTestId('review-notice')).not.toBeInTheDocument()
  } finally {
    vi.useRealTimers()
  }
})

test('the newer-console notice stays', async () => {
  const { send } = await renderReview()
  vi.useFakeTimers()
  try {
    send({ type: 'ready', v: 2 })
    act(() => { vi.advanceTimersByTime(10_000) })
    act(() => { screen.getByTestId('review-notice').click() })
    expect(screen.getByTestId('review-notice')).toHaveTextContent('this review page needs a newer console')
  } finally {
    vi.useRealTimers()
  }
})

test.each([
  [null, 'no open request'],
  [{ kind: 'plan-approval', content: { kind: 'unavailable', path: 'x', reason: 'file not found' }, revision: 'r1', answers: {} }, 'file not found'],
])('says why there is no page (%#)', async (value, text) => {
  req = value
  renderWithProviders(
    <Routes><Route path="/task/:repository/:issue/review" element={<ReviewPage />} /></Routes>,
    { route: '/task/widget/42/review' },
  )
  expect(await screen.findByText(text)).toBeInTheDocument()
  expect(screen.queryByTestId('review-frame')).not.toBeInTheDocument()
})

test('a new revision gets a new frame window: a late message of the old page posts nothing', async () => {
  const { frame, send, queryClient } = await renderReview()
  req = request('r2', '<p>second</p>')
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.request('widget', 42) }))
  await waitFor(() => expect(screen.getByTestId('review-frame')).toHaveAttribute('srcdoc', '<p>second</p>'))
  const next = screen.getByTestId('review-frame')
  expect(next).not.toBe(frame)
  send({ type: 'answers', v: 1, answers: { format: 'old' }, submit: null })
  act(() => { window.dispatchEvent(new Event('pagehide')) })
  await new Promise((r) => setTimeout(r, 50))
  expect(posts).toEqual([])
})

test('a request that closes clears the page and its bridge', async () => {
  const { send, queryClient } = await renderReview()
  req = null
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.request('widget', 42) }))
  expect(await screen.findByText('no open request')).toBeInTheDocument()
  expect(screen.queryByTestId('review-frame')).not.toBeInTheDocument()
  expect(screen.queryByTestId('review-bar')).not.toBeInTheDocument()
  send({ type: 'answers', v: 1, answers: { format: 'a' }, submit: null })
  act(() => { window.dispatchEvent(new Event('pagehide')) })
  await new Promise((r) => setTimeout(r, 50))
  expect(posts).toEqual([])
})

test('a post that fails shows the unsaved notice', async () => {
  server.use(http.post('/api/task/widget/42/answers', () => HttpResponse.json({ detail: 'x' }, { status: 500 })))
  await renderReview()
  fireEvent.click(screen.getByRole('button', { name: 'Send changes' }))
  expect(await screen.findByTestId('review-notice')).toHaveTextContent('could not save your selection; try again')
})

// Ticket 04: the Send bar shows the delivery of the tab's submission
// (`review-delivery`), offers Resend after a drop, and restores dropped
// answers into a changed page.
const PAGE = readFileSync('../tests/fixtures/review-page.html', 'utf8')
const CHANGED = 'the plan changed; your selections were reset to the saved ones'
const PAGE_CHANGED = 'page changed, check and resend'
const shown = () => screen.queryByTestId('review-delivery')
/** Every read of the route refetches, as a poll would. */
const poll = (queryClient: QueryClient) => act(() => queryClient.invalidateQueries())
const submissions = () => posts.filter((p) => (p as { submit: unknown }).submit !== null)

test('scenario 13: Send answers shows queued, then delivered; never sent, no Resend', async () => {
  req = { ...request('r1'), kind: 'answers' }
  const { queryClient } = await renderReview()
  fireEvent.click(screen.getByRole('button', { name: 'Send answers' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  await waitFor(() => expect(shown()).toHaveTextContent('queued'))
  expect(screen.queryByText(/\bsent\b/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resend' })).not.toBeInTheDocument()
  delivery = { state: 'queued', reason: '', submit: 'changes', revision: 'r1' }
  await poll(queryClient)
  expect(shown()).toHaveTextContent('queued')
  expect(screen.queryByRole('button', { name: 'Resend' })).not.toBeInTheDocument()
  delivery = { state: 'delivered', reason: '', submit: 'changes', revision: 'r1' }
  await poll(queryClient)
  await waitFor(() => expect(shown()).toHaveTextContent('delivered'))
  expect(shown()).not.toHaveTextContent('queued')
  expect(screen.queryByText(/\bsent\b/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resend' })).not.toBeInTheDocument()
})

test('scenario 14: a drop shows its reason, keeps the page, and Resend posts the same submission once', async () => {
  const { frame, send, queryClient } = await renderReview()
  send({ type: 'answers', v: 1, answers: { format: 'b' }, submit: null })
  fireEvent.click(screen.getByRole('button', { name: 'Send changes' }))
  await waitFor(() => expect(posts).toEqual([{ answers: { format: 'b' }, submit: 'changes', revision: 'r1' }]))
  delivery = { state: 'dropped', reason: 'no open request', submit: 'changes', revision: 'r1' }
  await poll(queryClient)
  await waitFor(() => expect(shown()).toHaveTextContent('dropped: no open request'))
  expect(screen.getByTestId('review-frame')).toBe(frame)
  expect(frame).toHaveAttribute('srcdoc', '<p>page</p>')
  fireEvent.click(screen.getByRole('button', { name: 'Resend' }))
  await waitFor(() => expect(posts).toHaveLength(2))
  await new Promise((r) => setTimeout(r, 50))
  expect(posts).toEqual([
    { answers: { format: 'b' }, submit: 'changes', revision: 'r1' },
    { answers: { format: 'b' }, submit: 'changes', revision: 'r1' },
  ])
})

test('Resend of a dropped approve arms first and posts only on the second tap', async () => {
  const { send, queryClient } = await renderReview()
  send({ type: 'answers', v: 1, answers: { format: 'a' }, submit: null })
  fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
  fireEvent.click(screen.getByRole('button', { name: 'Tap again to approve' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  delivery = { state: 'dropped', reason: 'no open request', submit: 'approve', revision: 'r1' }
  await poll(queryClient)
  fireEvent.click(await screen.findByRole('button', { name: 'Resend' }))
  await new Promise((r) => setTimeout(r, 50))
  expect(posts).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Tap again to approve' }))
  await waitFor(() => expect(posts).toHaveLength(2))
  expect(posts[1]).toEqual({ answers: { format: 'a' }, submit: 'approve', revision: 'r1' })
})

test('scenario 11: a drop of an earlier round, with nothing sent from this tab, shows no delivery', async () => {
  delivery = { state: 'dropped', reason: 'stale revision', submit: 'changes', revision: 'r0' }
  const { queryClient } = await renderReview()
  await poll(queryClient)
  await new Promise((r) => setTimeout(r, 50))
  expect(shown()).not.toBeInTheDocument()
  expect(screen.queryByText(/dropped/)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resend' })).not.toBeInTheDocument()
  // The same route does show the delivery of what this tab sends.
  fireEvent.click(screen.getByRole('button', { name: 'Send changes' }))
  await waitFor(() => expect(shown()).toHaveTextContent('queued'))
  expect(shown()).not.toHaveTextContent('dropped')
})

test('scenario 12: after the request closes, the route shows delivered, not no open request', async () => {
  req = { ...request('r1'), kind: 'answers' }
  const { queryClient } = await renderReview()
  fireEvent.click(screen.getByRole('button', { name: 'Send answers' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  req = null
  delivery = { state: 'delivered', reason: '', submit: 'changes', revision: 'r1' }
  await poll(queryClient)
  await waitFor(() => expect(screen.queryByTestId('review-frame')).not.toBeInTheDocument())
  await waitFor(() => expect(shown()).toHaveTextContent('delivered'))
  expect(screen.queryByText('no open request')).not.toBeInTheDocument()
})

test('scenario 18: a stale-revision drop restores the dropped answers into the new page and submits nothing', async () => {
  req = request('r1', PAGE)
  const { send, queryClient } = await renderReview()
  const dropped = { format: 'b', scope: 'wide', 'format.note': 'x' }
  send({ type: 'answers', v: 1, answers: dropped, submit: null })
  fireEvent.click(screen.getByRole('button', { name: 'Send changes' }))
  await waitFor(() => expect(posts).toEqual([{ answers: dropped, submit: 'changes', revision: 'r1' }]))
  const r2 = PAGE.replace('</body>', '<p id="rev">r2</p></body>')
  req = request('r2', r2, {})
  delivery = { state: 'dropped', reason: 'stale revision', submit: 'changes', revision: 'r1' }
  await poll(queryClient)
  await waitFor(() => expect(screen.getByTestId('review-frame')).toHaveAttribute('srcdoc', r2))
  expect(await screen.findByText(new RegExp(PAGE_CHANGED))).toBeInTheDocument()
  const win = (screen.getByTestId('review-frame') as HTMLIFrameElement).contentWindow!
  const restore = vi.spyOn(win, 'postMessage')
  act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type: 'ready', v: 1 }, source: win })) })
  expect(restore).toHaveBeenCalledWith(
    { type: 'restore', v: 1, answers: expect.objectContaining({ format: 'b', 'format.note': 'x' }) }, '*')
  await poll(queryClient)
  await new Promise((r) => setTimeout(r, 50))
  expect(submissions()).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Send changes' }))
  await waitFor(() => expect(submissions()).toHaveLength(2))
  expect(submissions()[1]).toMatchObject({ submit: 'changes', revision: 'r2' })
})

test('a revision change after a delivered submission still shows the changed notice and the saved answers', async () => {
  const { queryClient } = await renderReview()
  fireEvent.click(screen.getByRole('button', { name: 'Send changes' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  delivery = { state: 'delivered', reason: '', submit: 'changes', revision: 'r1' }
  await poll(queryClient)
  await waitFor(() => expect(shown()).toHaveTextContent('delivered'))
  req = request('r2', '<p>second</p>', { format: 'a' })
  await poll(queryClient)
  await waitFor(() => expect(screen.getByTestId('review-frame')).toHaveAttribute('srcdoc', '<p>second</p>'))
  expect(screen.getByTestId('review-notice')).toHaveTextContent(CHANGED)
  expect(screen.queryByText(new RegExp(PAGE_CHANGED))).not.toBeInTheDocument()
  const win = (screen.getByTestId('review-frame') as HTMLIFrameElement).contentWindow!
  const restore = vi.spyOn(win, 'postMessage')
  act(() => { window.dispatchEvent(new MessageEvent('message', { data: { type: 'ready', v: 1 }, source: win })) })
  expect(restore).toHaveBeenCalledWith({ type: 'restore', v: 1, answers: { format: 'a' } }, '*')
})
