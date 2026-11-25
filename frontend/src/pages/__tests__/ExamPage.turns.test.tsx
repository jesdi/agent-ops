import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse, type JsonBodyType } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { examView, question, SHA } from '../../test/exam'
import { ExamPage } from '../ExamPage'

// Interface under test (defined here, built by the ticket):
//   each question of the open attempt has a "Tip n / 3" button (n = the question's finished `tips`, 3 the view's
//   `tip_cap`) and an "Ask" button;
//   "Ask" opens the question's thread with a textbox named /message/ and a "Send" button.
//   Tip sends POST /api/exam/jesdi/general-skills/240/turns {attempt, head_sha, kind: 'tip', question, message}
//   where message is the question's answer draft ('' for a choice question); Send sends kind 'question'.
//   A 202 {id} is followed by reading GET .../turns/{id}/stream with fetch (jsdom has no EventSource):
//   `data: {"delta"}` events show in the question's li as they arrive, `data: {"status": "done"}` ends the
//   turn and the page reads the exam view again; `{"status": "failed", "error"}` shows the error and a "Retry".
//   A 429 or 422 `detail` shows in the question's li as it is. The view's questions carry `tips`, `assisted` and
//   `thread` [{id, kind, message, text, status, error}], shown inline in the question's li; a failed turn there
//   shows its `error` and a "Retry" that sends the same kind, question and message again.
const URL_PATH = '/api/exam/jesdi/general-skills/240'
const REPLY = 'Part one. Part two.'

const q1 = question({ id: 'q1', type: 'choice', prompt: 'Which SHA does the lease compare?', options: ['the head SHA', 'the base SHA'] })
const q2 = question({ id: 'q2', type: 'text', prompt: 'Why is the base SHA rechecked before merging?' })
const view = (second: object = q2) => examView([q1, second])
const done = { id: 't1', kind: 'tip', message: 'the token only grows', text: REPLY, status: 'done', error: null }

function renderExam() {
  return renderWithProviders(<Routes>
    <Route path="/exam/:owner/:name/:pr" element={<ExamPage />} />
  </Routes>, { route: '/exam/jesdi/general-skills/240' })
}
const serve = (body: JsonBodyType) => server.use(http.get(URL_PATH, () => HttpResponse.json(body)))
const questionOf = (prompt: string) => screen.getByText(prompt).closest('li') as HTMLElement

function sse() {
  const enc = new TextEncoder()
  let ctl!: ReadableStreamDefaultController<Uint8Array>
  const stream = new ReadableStream<Uint8Array>({ start: (c) => { ctl = c } })
  return {
    response: () => new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } }),
    send: (event: unknown) => ctl.enqueue(enc.encode(`data: ${JSON.stringify(event)}\n\n`)),
    end: () => ctl.close(),
  }
}

describe('exam page turns', () => {
  it('streams a tip chunk by chunk into its question, then counts it', async () => {
    let current: object = view()
    server.use(http.get(URL_PATH, () => HttpResponse.json(current)))
    const bodies: unknown[] = []
    const events = sse()
    server.use(
      http.post(`${URL_PATH}/turns`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: 't1' }, { status: 202 })
      }),
      http.get(`${URL_PATH}/turns/t1/stream`, () => events.response()),
    )
    renderExam()
    await screen.findByText(q2.prompt)
    await userEvent.type(within(questionOf(q2.prompt)).getByRole('textbox'), 'the token only grows')
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^tip 0 \/ 3$/i }))
    await waitFor(() => expect(bodies).toEqual([{ attempt: 1, head_sha: SHA, kind: 'tip', question: 'q2', message: 'the token only grows' }]))
    events.send({ delta: 'Part one. ' })
    expect(await within(questionOf(q2.prompt)).findByText(/Part one\./)).toBeInTheDocument()
    expect(within(questionOf(q2.prompt)).queryByText(/Part two/)).not.toBeInTheDocument()
    events.send({ delta: 'Part two.' })
    current = view({ ...q2, tips: 1, tips_left: 2, assisted: true, thread: [done] })
    events.send({ status: 'done' })
    events.end()
    expect(await within(questionOf(q2.prompt)).findByText(REPLY)).toBeInTheDocument()
    expect(within(questionOf(q2.prompt)).getByRole('button', { name: /^tip 1 \/ 3$/i })).toBeInTheDocument()
  })

  it('sends a message from the question thread and shows the reply under that question only', async () => {
    serve(view())
    const bodies: unknown[] = []
    const events = sse()
    server.use(
      http.post(`${URL_PATH}/turns`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: 't2' }, { status: 202 })
      }),
      http.get(`${URL_PATH}/turns/t2/stream`, () => events.response()),
    )
    renderExam()
    await screen.findByText(q2.prompt)
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^ask$/i }))
    await userEvent.type(within(questionOf(q2.prompt)).getByRole('textbox', { name: /message/i }), 'is the base SHA enough?')
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^send$/i }))
    await waitFor(() => expect(bodies).toEqual([{ attempt: 1, head_sha: SHA, kind: 'question', question: 'q2', message: 'is the base SHA enough?' }]))
    events.send({ delta: 'Part one. ' })
    expect(await within(questionOf(q2.prompt)).findByText(/Part one\./)).toBeInTheDocument()
    expect(within(questionOf(q1.prompt)).queryByText(/Part one\./)).not.toBeInTheDocument()
    events.end()
  })

  it('shows the thread of a reloaded exam inline under its own question', async () => {
    serve(view({ ...q2, tips: 1, tips_left: 2, assisted: true, thread: [done] }))
    renderExam()
    await screen.findByText(q2.prompt)
    expect(await within(questionOf(q2.prompt)).findByText(REPLY)).toBeInTheDocument()
    expect(within(questionOf(q2.prompt)).getByRole('button', { name: /^tip 1 \/ 3$/i })).toBeInTheDocument()
    expect(within(questionOf(q1.prompt)).queryByText(REPLY)).not.toBeInTheDocument()
  })

  it.each([
    [429, 'unavailable: usage limit'],
    [422, 'PR too large for chat'],
  ])('shows the host refusal %i in the question and runs no stream', async (status, detail) => {
    serve(view())
    let streams = 0
    server.use(
      http.post(`${URL_PATH}/turns`, () => HttpResponse.json({ detail }, { status })),
      http.get(`${URL_PATH}/turns/:id/stream`, () => { streams += 1; return new HttpResponse(null) }),
    )
    renderExam()
    await screen.findByText(q2.prompt)
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^tip 0 \/ 3$/i }))
    expect(await within(questionOf(q2.prompt)).findByText(detail)).toBeInTheDocument()
    expect(streams).toBe(0)
    expect(within(questionOf(q2.prompt)).getByRole('button', { name: /^tip 0 \/ 3$/i })).toBeInTheDocument()
  })

  it('shows the error of a stream that fails, with a retry that asks again', async () => {
    serve(view())
    const bodies: unknown[] = []
    const events = sse()
    server.use(
      http.post(`${URL_PATH}/turns`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: `t${bodies.length}` }, { status: 202 })
      }),
      http.get(`${URL_PATH}/turns/t1/stream`, () => events.response()),
    )
    renderExam()
    await screen.findByText(q2.prompt)
    await userEvent.type(within(questionOf(q2.prompt)).getByRole('textbox'), 'draft')
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^tip 0 \/ 3$/i }))
    events.send({ delta: 'Part one. ' })
    events.send({ status: 'failed', error: 'the model stopped: exit 3' })
    events.end()
    expect(await within(questionOf(q2.prompt)).findByText(/the model stopped: exit 3/)).toBeInTheDocument()
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^retry$/i }))
    await waitFor(() => expect(bodies).toHaveLength(2))
    expect(bodies[1]).toEqual(bodies[0])
  })

  it('shows a turn the console restart failed, with its error and a retry', async () => {
    const failed = { id: 't1', kind: 'tip', message: 'the token only grows', text: 'Part', status: 'failed', error: 'console restarted' }
    serve(view({ ...q2, tips: 0, assisted: false, thread: [failed] }))
    const bodies: unknown[] = []
    server.use(http.post(`${URL_PATH}/turns`, async ({ request }) => {
      bodies.push(await request.json())
      return HttpResponse.json({ detail: 'unavailable: usage limit' }, { status: 429 })
    }))
    renderExam()
    await screen.findByText(q2.prompt)
    expect(await within(questionOf(q2.prompt)).findByText(/console restarted/)).toBeInTheDocument()
    expect(within(questionOf(q2.prompt)).getByRole('button', { name: /^tip 0 \/ 3$/i })).toBeInTheDocument()
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^retry$/i }))
    await waitFor(() => expect(bodies).toEqual([{ attempt: 1, head_sha: SHA, kind: 'tip', question: 'q2', message: 'the token only grows' }]))
  })

  it('shows the code of a reply, fenced or a bare diff, in the monospace font, and its prose in the body font', async () => {
    const reply = 'Look here:\n```diff\n@@ -1 +1 @@\n+recheck_base(sha)\n```\nThen compare.\n\n+assert owner == me'
    serve(view({ ...q2, tips: 1, tips_left: 2, assisted: true, thread: [{ ...done, text: reply }] }))
    renderExam()
    const q = within(await screen.findByText(q2.prompt).then(() => questionOf(q2.prompt)))
    expect(q.getByText(/recheck_base\(sha\)/)).toHaveClass('font-mono', 'whitespace-pre-wrap')
    expect(q.getByText('+assert owner == me')).toHaveClass('font-mono')
    expect(q.getByText('Look here:')).not.toHaveClass('font-mono')
    expect(q.getByText(/Then compare\./)).not.toHaveClass('font-mono')
  })
})
