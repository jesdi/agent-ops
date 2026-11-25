import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse, type JsonBodyType } from 'msw'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ExamPage } from '../ExamPage'

// Interface under test (defined here, built by the ticket):
//   each question of the open attempt has a "Bypass" button, or "Undo bypass" while it is the bypassed one;
//   Bypass asks `window.confirm` first, and only on OK sends
//   POST|DELETE /api/exam/jesdi/general-skills/240/bypass {attempt, head_sha, question}, answered with the exam view;
//   the view's `bypassed` is the bypassed question id or null;
//   a bypassed question needs no answer to submit; a result question with help "bypassed" shows
//   "bypassed", its explanation and its hunk, and counts in neither score nor max.
const SHA = 'a'.repeat(40)
const URL_PATH = '/api/exam/jesdi/general-skills/240'

const q1 = { id: 'q1', type: 'choice', prompt: 'Which SHA does the lease compare?', options: ['the head SHA', 'the base SHA'] }
const q2 = { id: 'q2', type: 'text', prompt: 'Why is the base SHA rechecked before merging?' }
const open = {
  repo: 'jesdi/general-skills', pr: 240, state: 'open', head_sha: SHA, debt: false, detail: 'attempt 1, 2 questions',
  attempt: 1, bypassed: null, exam: { version: 1, repo: 'jesdi/general-skills', pr: 240, head_sha: SHA, questions: [q1, q2] }, attempts: [],
}

function renderExam() {
  return renderWithProviders(<Routes>
    <Route path="/exam/:owner/:name/:pr" element={<ExamPage />} />
  </Routes>, { route: '/exam/jesdi/general-skills/240' })
}
const serve = (body: JsonBodyType) => server.use(http.get(URL_PATH, () => HttpResponse.json(body)))
const questionOf = (prompt: string) => screen.getByText(prompt).closest('li') as HTMLElement
const confirmWith = (answer: boolean) => vi.spyOn(window, 'confirm').mockReturnValue(answer)

afterEach(() => vi.restoreAllMocks())

describe('exam page bypass', () => {
  it('asks for confirmation, sends nothing when declined, and bypasses the question once confirmed', async () => {
    serve(open)
    const bodies: unknown[] = []
    server.use(http.post(`${URL_PATH}/bypass`, async ({ request }) => {
      bodies.push(await request.json())
      return HttpResponse.json({ ...open, bypassed: 'q2' })
    }))
    const ask = confirmWith(false)
    renderExam()
    await screen.findByText(q2.prompt)
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^bypass$/i }))
    expect(ask).toHaveBeenCalledTimes(1)
    expect(bodies).toEqual([])
    ask.mockReturnValue(true)
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /^bypass$/i }))
    await waitFor(() => expect(bodies).toEqual([{ attempt: 1, head_sha: SHA, question: 'q2' }]))
    expect(await within(questionOf(q2.prompt)).findByRole('button', { name: /undo bypass/i })).toBeInTheDocument()
  })

  it('undoes the bypass, and leaves a second question unbypassed while the first is', async () => {
    serve({ ...open, bypassed: 'q2' })
    const deletes: unknown[] = []
    server.use(
      http.delete(`${URL_PATH}/bypass`, async ({ request }) => {
        deletes.push(await request.json())
        return HttpResponse.json(open)
      }),
      http.post(`${URL_PATH}/bypass`, () => HttpResponse.json({ detail: 'another question is bypassed' }, { status: 409 })),
    )
    confirmWith(true)
    renderExam()
    await screen.findByText(q1.prompt)
    await userEvent.click(within(questionOf(q1.prompt)).getByRole('button', { name: /^bypass$/i }))
    await waitFor(() => expect(within(questionOf(q1.prompt)).getByRole('button', { name: /^bypass$/i })).toBeInTheDocument())
    expect(within(questionOf(q1.prompt)).queryByRole('button', { name: /undo bypass/i })).not.toBeInTheDocument()
    await userEvent.click(within(questionOf(q2.prompt)).getByRole('button', { name: /undo bypass/i }))
    await waitFor(() => expect(deletes).toEqual([{ attempt: 1, head_sha: SHA, question: 'q2' }]))
    expect(await within(questionOf(q2.prompt)).findByRole('button', { name: /^bypass$/i })).toBeInTheDocument()
  })

  it('submits with the bypassed question unanswered', async () => {
    serve({ ...open, bypassed: 'q2' })
    const bodies: unknown[] = []
    server.use(http.post(`${URL_PATH}/answers`, async ({ request }) => {
      bodies.push(await request.json())
      return HttpResponse.json({ ...open, state: 'grading', exam: null }, { status: 202 })
    }))
    renderExam()
    await screen.findByText(q1.prompt)
    const submit = screen.getByRole('button', { name: /submit/i })
    expect(submit).toBeDisabled()
    await userEvent.click(screen.getByRole('radio', { name: 'the base SHA' }))
    expect(submit).toBeEnabled()
    await userEvent.click(submit)
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ attempt: 1, head_sha: SHA, answers: { q1: 1 } })
  })

  it('shows a bypassed question in the result with its explanation and hunk, outside the points', async () => {
    const hunk = '@@ -40,2 +40,3 @@ base\n+recheck_base(sha)'
    const graded = [1, 2, 3, 4, 5].map((n) => ({ id: `p${n}`, concept: `concept-${n}`, points: 1 }))
    serve({
      ...open, state: 'passed', exam: null,
      attempts: [{
        attempt: 1, questions: [], result: {
          passed: true, score: 5, max: 5,
          questions: [...graded, { id: 'q6', concept: 'base-sha-recheck', help: 'bypassed', explanation: 'Main can move between grading and merge.', hunk }],
        },
      }],
    })
    renderExam()
    expect(await screen.findByText(/\b5\b.*\/.*\b5\b/)).toBeInTheDocument()
    expect(screen.getByText(/passed/i)).toBeInTheDocument()
    expect(screen.getByText(/bypassed/i)).toBeInTheDocument()
    expect(screen.getByText('Main can move between grading and merge.')).toBeInTheDocument()
    expect(screen.getByText(/recheck_base\(sha\)/)).toBeInTheDocument()
  })
})
