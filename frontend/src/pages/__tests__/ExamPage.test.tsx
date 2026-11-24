import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse, type JsonBodyType } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { onPhone } from '../../test/viewport'
import { ExamPage } from '../ExamPage'

const SHA = 'a'.repeat(40)
const URL_PATH = '/api/exam/jesdi/general-skills/240'

const q1 = { id: 'q1', type: 'choice', prompt: 'Which SHA does the lease compare?', options: ['the head SHA', 'the base SHA', 'the merge SHA'] }
const q2 = { id: 'q2', type: 'text', prompt: 'Why is the base SHA rechecked before merging?' }
const open = {
  repo: 'jesdi/general-skills', pr: 240, state: 'open', head_sha: SHA, debt: false, detail: 'attempt 1, 2 questions',
  attempt: 1, exam: { version: 1, repo: 'jesdi/general-skills', pr: 240, head_sha: SHA, questions: [q1, q2] }, results: [],
}

function renderExam() {
  return renderWithProviders(<Routes>
    <Route path="/exam/:owner/:name/:pr" element={<ExamPage />} />
  </Routes>, { route: '/exam/jesdi/general-skills/240' })
}
const serve = (body: JsonBodyType, status = 200) => server.use(http.get(URL_PATH, () => HttpResponse.json(body, { status })))
const submit = () => screen.getByRole('button', { name: /submit/i })

async function answerAll() {
  await userEvent.click(await screen.findByRole('radio', { name: 'the base SHA' }))
  await userEvent.type(screen.getByRole('textbox', { name: q2.prompt }), 'Because main may have moved.')
}

describe('exam page', () => {
  it('shows every prompt, radios for choice and a text area for free text, none preselected', async () => {
    serve(open)
    renderExam()
    expect(await screen.findByText(q1.prompt)).toBeInTheDocument()
    expect(screen.getByText(q2.prompt)).toBeInTheDocument()
    const radios = screen.getAllByRole('radio')
    expect(radios.map((r) => (r as HTMLInputElement).labels?.[0]?.textContent)).toEqual(q1.options)
    radios.forEach((r) => expect(r).not.toBeChecked())
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
  })

  it('keeps submit disabled until every question is answered, then sends the attempt, head SHA and answers once', async () => {
    serve(open)
    const bodies: unknown[] = []
    server.use(http.post(`${URL_PATH}/answers`, async ({ request }) => {
      bodies.push(await request.json())
      return HttpResponse.json({ ...open, state: 'grading', exam: null }, { status: 202 })
    }))
    renderExam()
    await screen.findByText(q1.prompt)
    expect(submit()).toBeDisabled()
    await userEvent.click(screen.getByRole('radio', { name: 'the base SHA' }))
    expect(submit()).toBeDisabled()
    await userEvent.type(screen.getByRole('textbox'), 'Because main may have moved.')
    expect(submit()).toBeEnabled()
    await userEvent.dblClick(submit())
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ attempt: 1, head_sha: SHA, answers: { q1: 1, q2: 'Because main may have moved.' } })
  })

  it('says it is generating or grading and refreshes by itself until the state changes', async () => {
    let calls = 0
    server.use(http.get(URL_PATH, () => {
      calls += 1
      return HttpResponse.json(calls < 2 ? { ...open, state: 'generating', exam: null } : open)
    }))
    renderExam()
    expect(await screen.findByText(/generating/i)).toBeInTheDocument()
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    expect(await screen.findByText(q1.prompt, {}, { timeout: 9000 })).toBeInTheDocument()
    expect(calls).toBeGreaterThan(1)
  }, 12000)

  it('says grading is under way while the state is grading', async () => {
    serve({ ...open, state: 'grading' })
    renderExam()
    expect(await screen.findByText(/grading/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /submit/i })).not.toBeInTheDocument()
  })

  it('after a pass shows the score and that exam is posted', async () => {
    serve({
      ...open, state: 'passed', exam: null,
      results: [{ attempt: 1, passed: true, score: 9, max: 10, questions: [] }],
    })
    renderExam()
    expect(await screen.findByText(/\b9\b.*\b10\b/)).toBeInTheDocument()
    expect(screen.getByText(/passed/i)).toBeInTheDocument()
    expect(screen.getByText(/exam.*(posted|success)/i)).toBeInTheDocument()
  })

  it('after a pass of a debt exam does not say a status is posted', async () => {
    serve({
      ...open, debt: true, state: 'passed', exam: null,
      results: [{ attempt: 1, passed: true, score: 9, max: 10, questions: [] }],
    })
    renderExam()
    expect(await screen.findByText(/\b9\b.*\b10\b/)).toBeInTheDocument()
    expect(screen.getByText(/passed/i)).toBeInTheDocument()
    expect(screen.queryByText(/posted|success/i)).not.toBeInTheDocument()
  })

  it('says the debt is cleared, not that there is no exam, once a graded debt exam is gone', async () => {
    let calls = 0
    server.use(http.get(URL_PATH, () => {
      calls += 1
      return calls < 2
        ? HttpResponse.json({ ...open, debt: true, state: 'grading', exam: null })
        : HttpResponse.json({ detail: 'exam not found' }, { status: 404 })
    }))
    renderExam()
    expect(await screen.findByText(/grading/i)).toBeInTheDocument()
    expect(await screen.findByText(/debt.*cleared/i, {}, { timeout: 9000 })).toBeInTheDocument()
    expect(screen.queryByText(/no exam for/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/posted|success/i)).not.toBeInTheDocument()
    const seen = calls
    await new Promise((r) => setTimeout(r, 3500))
    expect(calls).toBe(seen)   // a gone exam is not polled again
  }, 16000)

  it('after a fail shows each miss with its explanation and hunk, then the follow-up questions', async () => {
    const hunk1 = '@@ -10,3 +10,4 @@ lease\n+assert owner == me'
    const hunk2 = '@@ -40,2 +40,3 @@ base\n+recheck_base(sha)'
    const followUp = { id: 'q3', type: 'choice', prompt: 'Who owns the lease after a restart?', options: ['the old runner', 'the new runner'] }
    serve({
      ...open, attempt: 2, detail: 'attempt 2, 1 question',
      exam: { version: 1, repo: open.repo, pr: 240, head_sha: SHA, questions: [followUp] },
      results: [{
        attempt: 1, passed: false, score: 7, max: 10,
        questions: [
          { id: 'q0', concept: 'lease-expiry', points: 1 },
          { id: 'q1', concept: 'lease-ownership', points: 0, explanation: 'The lease belongs to the runner that took it.', hunk: hunk1 },
          { id: 'q2', concept: 'base-sha-recheck', points: 0.5, explanation: 'Main can move between grading and merge.', hunk: hunk2 },
        ],
      }],
    })
    renderExam()
    expect(await screen.findByText('The lease belongs to the runner that took it.')).toBeInTheDocument()
    expect(screen.getByText('Main can move between grading and merge.')).toBeInTheDocument()
    expect(screen.getByText(/assert owner == me/)).toBeInTheDocument()
    expect(screen.getByText(/recheck_base\(sha\)/)).toBeInTheDocument()
    expect(screen.getByText(followUp.prompt)).toBeInTheDocument()
    expect(screen.getAllByRole('radio')).toHaveLength(2)
    const miss = screen.getByText('The lease belongs to the runner that took it.')
    const followUpNode = screen.getByText(followUp.prompt)
    expect(miss.compareDocumentPosition(followUpNode) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows the grader feedback under a partial free-text question explanation, and none on a choice or passed one', async () => {
    serve({
      ...open, state: 'passed', exam: null,
      results: [{
        attempt: 1, passed: false, score: 2.5, max: 4,
        questions: [
          { id: 'q1', concept: 'lease-ownership', points: 0, explanation: 'The lease belongs to the runner that took it.', hunk: '@@ -1 +1 @@\n+x' },
          { id: 'q5', concept: 'token-growth', points: 0.5, explanation: 'The token only ever grows.', hunk: '@@ -2 +2 @@\n+y', feedback: 'misses that the token only grows' },
          { id: 'q6', concept: 'base-sha-recheck', points: 1 },
        ],
      }],
    })
    renderExam()
    const feedback = await screen.findByText(/misses that the token only grows/)
    const explanation = screen.getByText('The token only ever grows.')
    expect(explanation.compareDocumentPosition(feedback) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getAllByText(/misses that/)).toHaveLength(1)
    expect(screen.getByText('The lease belongs to the runner that took it.')).toBeInTheDocument()
  })

  it('on a 409 says the exam changed and reloads it', async () => {
    const reloaded = { ...open, head_sha: 'b'.repeat(40), attempt: 1, exam: { ...open.exam, questions: [{ ...q2, id: 'q9', prompt: 'A brand new question?' }] } }
    let gets = 0
    server.use(
      http.get(URL_PATH, () => HttpResponse.json(gets++ === 0 ? open : reloaded)),
      http.post(`${URL_PATH}/answers`, () => HttpResponse.json({ detail: 'exam changed' }, { status: 409 })),
    )
    renderExam()
    await answerAll()
    await userEvent.click(submit())
    expect(await screen.findByText(/exam (has )?changed/i)).toBeInTheDocument()
    expect(await screen.findByText('A brand new question?')).toBeInTheDocument()
    expect(screen.queryByText(q1.prompt)).not.toBeInTheDocument()
  })

  it('shows the detail on error', async () => {
    serve({ ...open, state: 'error', exam: null, detail: 'the grading session timed out' })
    renderExam()
    expect(await screen.findByText(/the grading session timed out/)).toBeInTheDocument()
  })

  it('says there is no exam for the PR on a 404', async () => {
    serve({ detail: 'no exam' }, 404)
    renderExam()
    expect(await screen.findByText(/no exam for/i)).toBeInTheDocument()
  })

  it('says the exam is unavailable on another failure', async () => {
    serve({ detail: 'state unreadable' }, 500)
    renderExam()
    expect(await screen.findByText(/exam unavailable/i)).toBeInTheDocument()
  })

  it('shows why a submission failed when it was not a conflict, and keeps the answers', async () => {
    serve(open)
    server.use(http.post(`${URL_PATH}/answers`, () => HttpResponse.json({ detail: 'answers rejected' }, { status: 422 })))
    renderExam()
    await answerAll()
    await userEvent.click(submit())
    expect(await screen.findByRole('alert')).toHaveTextContent('answers rejected')
    expect(screen.queryByText(/exam (has )?changed/i)).not.toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'the base SHA' })).toBeChecked()
  })

  it('marks a debt exam as debt', async () => {
    serve({ ...open, debt: true })
    renderExam()
    expect(await screen.findByText(/\bdebt\b/i)).toBeInTheDocument()
  })

  it('does not mark a normal exam as debt', async () => {
    serve(open)
    renderExam()
    await screen.findByText(q1.prompt)
    expect(screen.queryByText(/\bdebt\b/i)).not.toBeInTheDocument()
  })

  it('is usable by keyboard alone at phone width', async () => {
    onPhone()
    serve(open)
    server.use(http.post(`${URL_PATH}/answers`, () => HttpResponse.json({ ...open, state: 'grading', exam: null }, { status: 202 })))
    renderExam()
    const group = await screen.findByRole('radiogroup', { name: q1.prompt })
    expect(within(group).getAllByRole('radio')).toHaveLength(3)
    await userEvent.tab()
    await userEvent.keyboard('{ArrowDown}')
    expect(screen.getByRole('radio', { name: 'the base SHA' })).toBeChecked()
    await userEvent.tab()
    await userEvent.keyboard('Because main may have moved.')
    await userEvent.tab()
    expect(submit()).toHaveFocus()
    await userEvent.keyboard('{Enter}')
    expect(await screen.findByText(/grading/i)).toBeInTheDocument()
  })
})
