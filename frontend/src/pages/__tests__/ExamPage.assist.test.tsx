import { screen, within } from '@testing-library/react'
import { http, HttpResponse, type JsonBodyType } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ExamPage } from '../ExamPage'

// Interface under test (defined here, built by the ticket), on top of ExamPage.turns.test.tsx:
//   a question whose `tips` is 3 shows its tip button as "Tip 3 / 3", disabled; below 3 it stays enabled;
//   a question with `assisted: true` shows the text "assisted" in its li, one without shows none;
//   while the attempt is grading (state 'grading', exam null) each question of attempts[last].questions
//   still shows in its li with its "Ask" button and its inline thread, and has no tip button at all.
const SHA = 'a'.repeat(40)
const URL_PATH = '/api/exam/jesdi/general-skills/240'
const REPLY = 'Part one. Part two.'
const tip = { id: 't1', kind: 'tip', message: 'hint', text: REPLY, status: 'done', error: null }
const chat = { id: 't2', kind: 'question', message: 'why?', text: 'Because it grows.', status: 'done', error: null }

const q1 = { id: 'q1', type: 'choice', prompt: 'Which SHA does the lease compare?', options: ['the head SHA', 'the base SHA'], tips: 0, assisted: false, thread: [] }
const q2 = { id: 'q2', type: 'text', prompt: 'Why is the base SHA rechecked before merging?', tips: 0, assisted: false, thread: [] }
const view = (first: object, second: object) => ({
  repo: 'jesdi/general-skills', pr: 240, state: 'open', head_sha: SHA, debt: false, detail: 'attempt 1, 2 questions',
  attempt: 1, bypassed: null, exam: { version: 1, repo: 'jesdi/general-skills', pr: 240, head_sha: SHA, questions: [first, second] }, attempts: [],
})

function renderExam() {
  return renderWithProviders(<Routes>
    <Route path="/exam/:owner/:name/:pr" element={<ExamPage />} />
  </Routes>, { route: '/exam/jesdi/general-skills/240' })
}
const serve = (body: JsonBodyType) => server.use(http.get(URL_PATH, () => HttpResponse.json(body)))
const questionOf = (prompt: string) => screen.getByText(prompt).closest('li') as HTMLElement

describe('exam page tip cap, assisted mark and grading', () => {
  it('disables the tip button of a question that has had its three tips, and only that one', async () => {
    serve(view({ ...q1, tips: 2, assisted: true, thread: [tip, tip] }, { ...q2, tips: 3, assisted: true, thread: [tip, tip, tip] }))
    renderExam()
    await screen.findByText(q2.prompt)
    expect(within(questionOf(q2.prompt)).getByRole('button', { name: /^tip 3 \/ 3$/i })).toBeDisabled()
    expect(within(questionOf(q1.prompt)).getByRole('button', { name: /^tip 2 \/ 3$/i })).toBeEnabled()
  })

  it('marks an assisted question as assisted and leaves the others unmarked', async () => {
    serve(view({ ...q1, tips: 0, assisted: false }, { ...q2, tips: 1, assisted: true, thread: [tip] }))
    renderExam()
    await screen.findByText(q2.prompt)
    expect(within(questionOf(q2.prompt)).getByText(/assisted/i)).toBeInTheDocument()
    expect(within(questionOf(q1.prompt)).queryByText(/assisted/i)).not.toBeInTheDocument()
  })

  it('while grading shows no tip button, keeps Ask and the question thread inline', async () => {
    const asked = { ...q2, tips: 1, assisted: true, thread: [tip, chat] }
    serve({
      ...view(q1, asked), state: 'grading', exam: null,
      attempts: [{ attempt: 1, questions: [q1, asked], result: null }],
    })
    renderExam()
    expect(await screen.findByText(/grading/i)).toBeInTheDocument()
    expect(await screen.findByText(q2.prompt)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /tip/i })).not.toBeInTheDocument()
    expect(within(questionOf(q2.prompt)).getByRole('button', { name: /^ask$/i })).toBeEnabled()
    expect(within(questionOf(q2.prompt)).getByText('Because it grows.')).toBeInTheDocument()
  })
})
