import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse, type JsonBodyType } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ExamPage } from '../ExamPage'

// The general chat, selection quotes, the threads of submitted attempts, the assisted mark on a result, and a running
// turn found by a reload (pr-exam-host-assist requirements 2, 4, 10, 11 and 12).
const SHA = 'a'.repeat(40)
const URL_PATH = '/api/exam/jesdi/general-skills/240'

const q1 = { id: 'q1', type: 'choice', prompt: 'Which SHA does the lease compare?', options: ['the head SHA', 'the base SHA'], tips: 0, assisted: false, thread: [] }
const q5 = { id: 'q5', type: 'text', prompt: 'Why does the token only grow?', tips: 0, assisted: false, thread: [] }
const open = {
  repo: 'jesdi/general-skills', pr: 240, state: 'open', head_sha: SHA, debt: false, detail: 'attempt 1, 2 questions',
  attempt: 1, bypassed: null, exam: { version: 1, repo: 'jesdi/general-skills', pr: 240, head_sha: SHA, questions: [q1, q5] }, attempts: [],
}
const general = { id: 'g1', kind: 'general', message: 'what is the riskiest change?', text: 'The lease expiry.', status: 'done', error: null }

function renderExam() {
  return renderWithProviders(<Routes>
    <Route path="/exam/:owner/:name/:pr" element={<ExamPage />} />
  </Routes>, { route: '/exam/jesdi/general-skills/240' })
}
const serve = (body: JsonBodyType) => server.use(http.get(URL_PATH, () => HttpResponse.json(body)))
const questionOf = (prompt: string) => screen.getByText(prompt).closest('li') as HTMLElement
const drawer = () => screen.getByRole('complementary', { name: /general chat/i })

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

function startTurns(bodies: unknown[], id = 'g2') {
  server.use(http.post(`${URL_PATH}/turns`, async ({ request }) => {
    bodies.push(await request.json())
    return HttpResponse.json({ id }, { status: 202 })
  }))
}

function select(node: Node) {
  act(() => {
    const range = document.createRange()
    range.selectNodeContents(node)
    const selection = document.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)
    document.dispatchEvent(new Event('selectionchange'))
  })
}

describe('exam page general chat', () => {
  it('opens a drawer that shows the stored general chat and sends a general turn', async () => {
    serve({ ...open, exam: { ...open.exam, general: [general] } })
    const bodies: unknown[] = []
    startTurns(bodies)
    const events = sse()
    server.use(http.get(`${URL_PATH}/turns/g2/stream`, () => events.response()))
    renderExam()
    await screen.findByText(q5.prompt)
    expect(screen.queryByRole('complementary', { name: /general chat/i })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /^chat$/i }))
    expect(within(drawer()).getByText('The lease expiry.')).toBeInTheDocument()
    await userEvent.type(within(drawer()).getByRole('textbox', { name: /message/i }), 'and the second riskiest?')
    await userEvent.click(within(drawer()).getByRole('button', { name: /^send$/i }))
    await waitFor(() => expect(bodies).toEqual([{ attempt: 1, head_sha: SHA, kind: 'general', question: null, message: 'and the second riskiest?' }]))
    expect(within(drawer()).getByRole('textbox', { name: /message/i })).toHaveValue('')
    events.send({ delta: 'The retry budget.' })
    expect(await within(drawer()).findByText(/The retry budget\./)).toBeInTheDocument()
    events.end()
  })

  it('is a bottom sheet below md and a drawer on the right from md', async () => {
    serve(open)
    renderExam()
    await screen.findByText(q5.prompt)
    await userEvent.click(screen.getByRole('button', { name: /^chat$/i }))
    expect(drawer()).toHaveClass('fixed', 'inset-x-0', 'bottom-0', 'md:inset-y-0', 'md:left-auto', 'md:w-96')
  })

  it('keeps a message that was refused, to send again', async () => {
    serve(open)
    server.use(http.post(`${URL_PATH}/turns`, () => HttpResponse.json({ detail: 'unavailable: usage limit' }, { status: 429 })))
    renderExam()
    await screen.findByText(q5.prompt)
    await userEvent.click(within(questionOf(q5.prompt)).getByRole('button', { name: /^ask$/i }))
    await userEvent.type(within(questionOf(q5.prompt)).getByRole('textbox', { name: /message/i }), 'why?')
    await userEvent.click(within(questionOf(q5.prompt)).getByRole('button', { name: /^send$/i }))
    expect(await within(questionOf(q5.prompt)).findByText('unavailable: usage limit')).toBeInTheDocument()
    expect(within(questionOf(q5.prompt)).getByRole('textbox', { name: /message/i })).toHaveValue('why?')
  })
})

describe('exam page selection quotes', () => {
  it('pre-fills the thread of the question a selection is in, and the general chat for one in the header', async () => {
    serve(open)
    renderExam()
    await screen.findByText(q1.prompt)
    select(screen.getByText(q1.prompt).firstChild!)
    expect(within(questionOf(q1.prompt)).getByRole('textbox', { name: /message/i })).toHaveValue(`> ${q1.prompt}\n\n`)
    expect(within(questionOf(q5.prompt)).queryByRole('textbox', { name: /message/i })).not.toBeInTheDocument()
    select(screen.getByRole('heading', { level: 1 }).firstChild!)
    expect(within(drawer()).getByRole('textbox', { name: /message/i })).toHaveValue('> Exam for\n\n')
  })

  it('pre-fills the thread of a graded question from a selection in its result', async () => {
    const explanation = 'The token only ever grows.'
    serve({
      ...open, state: 'passed', exam: null,
      attempts: [{ attempt: 1, questions: [q1, q5], result: { passed: true, score: 1.5, max: 2, questions: [
        { id: 'q1', concept: 'lease', points: 1 },
        { id: 'q5', concept: 'token', points: 0.5, explanation, hunk: '@@ -1 +1 @@\n+y', feedback: 'misses that it grows' },
      ] } }],
    })
    renderExam()
    select((await screen.findByText(explanation)).firstChild!)
    expect(within(questionOf(q5.prompt)).getByRole('textbox', { name: /message/i })).toHaveValue(`> ${explanation}\n\n`)
  })
})

describe('exam page threads of submitted attempts', () => {
  it('asks why in the failed attempt 1 thread while attempt 2 is open, and sends attempt 1', async () => {
    const answeredWrong = { ...q5, thread: [] }
    serve({
      ...open, attempt: 2, exam: { ...open.exam, questions: [{ ...q5, prompt: 'Who may grow the token?' }] },
      attempts: [{ attempt: 1, questions: [q1, answeredWrong], result: { passed: false, score: 1, max: 2, questions: [
        { id: 'q1', concept: 'lease', points: 1 },
        { id: 'q5', concept: 'token', points: 0, explanation: 'It only grows.', hunk: '@@', feedback: 'misses that the token only grows' },
      ] } }],
    })
    const bodies: unknown[] = []
    startTurns(bodies, 't1')
    renderExam()
    await screen.findByText(q5.prompt)
    const failed = questionOf(q5.prompt)
    expect(within(failed).getByText('misses that the token only grows')).toBeInTheDocument()
    expect(within(failed).queryByRole('button', { name: /tip/i })).not.toBeInTheDocument()
    await userEvent.click(within(failed).getByRole('button', { name: /^ask$/i }))
    await userEvent.type(within(failed).getByRole('textbox', { name: /message/i }), 'why was I wrong?')
    await userEvent.click(within(failed).getByRole('button', { name: /^send$/i }))
    await waitFor(() => expect(bodies).toEqual([{ attempt: 1, head_sha: SHA, kind: 'question', question: 'q5', message: 'why was I wrong?' }]))
  })

  it('keeps the question threads of a passed attempt, with Ask', async () => {
    const asked = { ...q5, thread: [{ id: 't1', kind: 'question', message: 'why?', text: 'Because it grows.', status: 'done', error: null }] }
    serve({
      ...open, state: 'passed', exam: null,
      attempts: [{ attempt: 1, questions: [q1, asked], result: { passed: true, score: 2, max: 2, questions: [
        { id: 'q1', concept: 'lease', points: 1 }, { id: 'q5', concept: 'token', points: 1 }] } }],
    })
    renderExam()
    await screen.findByText(q5.prompt)
    expect(within(questionOf(q5.prompt)).getByText('Because it grows.')).toBeInTheDocument()
    expect(within(questionOf(q5.prompt)).getByRole('button', { name: /^ask$/i })).toBeEnabled()
  })

  it.each([true, false])('marks an assisted question on a graded result (passed: %s), full points included', async (passed) => {
    serve({
      ...open, state: passed ? 'passed' : 'generating', exam: null,
      attempts: [{ attempt: 1, questions: [q1, q5], result: { passed, score: 1, max: 2, questions: [
        { id: 'q1', concept: 'lease', points: 1, help: 'assisted' }, { id: 'q5', concept: 'token', points: 0, explanation: 'x', hunk: '@@' }] } }],
    })
    renderExam()
    await screen.findByText(q1.prompt)
    expect(within(questionOf(q1.prompt)).getByText(/assisted/)).toBeInTheDocument()
    expect(within(questionOf(q5.prompt)).queryByText(/assisted/)).not.toBeInTheDocument()
  })

  it('marks an assisted question on a result kept without its questions', async () => {
    serve({
      ...open, state: 'passed', exam: null,
      attempts: [{ attempt: 1, questions: [], result: { passed: true, score: 1, max: 1, questions: [
        { id: 'q1', concept: 'lease-ownership', points: 1, help: 'assisted' }] } }],
    })
    renderExam()
    expect(await screen.findByText('lease-ownership')).toBeInTheDocument()
    expect(screen.getByText('assisted')).toBeInTheDocument()
  })
})

describe('exam page running turn found by a reload', () => {
  it('watches the stream of a running turn, then reads the stored reply', async () => {
    const running = { id: 't9', kind: 'tip', message: 'hint', text: '', status: 'running', error: null }
    let current: object = { ...open, exam: { ...open.exam, questions: [q1, { ...q5, tips: 1, thread: [running] }] } }
    server.use(http.get(URL_PATH, () => HttpResponse.json(current)))
    const events = sse()
    server.use(http.get(`${URL_PATH}/turns/t9/stream`, () => events.response()))
    renderExam()
    await screen.findByText(q5.prompt)
    events.send({ delta: 'Part one. ' })
    expect(await within(questionOf(q5.prompt)).findByText(/Part one\./)).toBeInTheDocument()
    expect(within(questionOf(q5.prompt)).getByRole('button', { name: /^tip 1 \/ 3$/i })).toBeDisabled()
    current = { ...open, exam: { ...open.exam, questions: [q1, { ...q5, tips: 1, assisted: true, thread: [{ ...running, status: 'done', text: 'Part one. Part two.' }] }] } }
    events.send({ status: 'done' })
    events.end()
    expect(await within(questionOf(q5.prompt)).findByText('Part one. Part two.')).toBeInTheDocument()
    expect(within(questionOf(q5.prompt)).getByRole('button', { name: /^tip 1 \/ 3$/i })).toBeEnabled()
  })
})
