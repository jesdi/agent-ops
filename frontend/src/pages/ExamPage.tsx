import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router'
import { api, ApiError, type ExamQuestion, type ExamResult, type ExamState, type ExamView } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'
import { useExam } from '../hooks/useResources'
import { ExamThread, type ChatTarget } from '../components/ExamThread'

type Answers = Record<string, number | string>

type Bypassing = { bypassed: string | null; toggle: (question: string) => void }

function Question({ q, value, onChange, bypassing, chat }: {
  q: ExamQuestion; value: number | string | undefined; onChange: (v: number | string) => void; bypassing: Bypassing; chat: ChatTarget
}) {
  const promptId = `prompt-${q.id}`
  const bypassed = bypassing.bypassed === q.id
  const draft = typeof value === 'string' ? value : ''
  return (
    <li className="space-y-2">
      <p id={promptId} className="font-medium">{q.prompt}</p>
      {q.type === 'choice' ? (
        <div role="radiogroup" aria-labelledby={promptId} className="space-y-1">
          {(q.options ?? []).map((option, i) => (
            <label key={option} className="flex items-center gap-2 py-1">
              <input type="radio" name={q.id} checked={value === i} onChange={() => onChange(i)} />
              {option}
            </label>
          ))}
        </div>
      ) : (
        <textarea
          aria-labelledby={promptId}
          rows={4}
          value={draft}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded border border-ink-muted bg-transparent p-2"
        />
      )}
      <button type="button" onClick={() => bypassing.toggle(q.id)} className="rounded border px-2 py-1 text-sm">
        {bypassed ? 'Undo bypass' : 'Bypass'}
      </button>
      <Assisted q={q} />
      <ExamThread q={q} draft={draft} target={chat} />
    </li>
  )
}

function Assisted({ q }: { q: ExamQuestion }) {
  return q.assisted ? <p className="text-sm text-ink-muted">assisted</p> : null
}

/** The questions of `result` that lost points or were bypassed, each with its explanation, the grader's feedback and its hunk. */
function Misses({ result }: { result: ExamResult }) {
  return result.questions.filter((m) => m.help === 'bypassed' || (m.points ?? 1) < 1).map((m) => (
    <article key={m.id} className="space-y-1">
      <h2 className="font-medium">{m.concept}</h2>
      <p>{m.explanation}</p>
      {m.feedback && <p className="text-sm">{m.feedback}</p>}
      <pre className="overflow-x-auto rounded border border-border bg-surface p-2 text-sm">{m.hunk}</pre>
    </article>
  ))
}

function isStatus(error: unknown, status: number) {
  return error instanceof ApiError && error.status === status
}

function isAnswered(questions: ExamQuestion[], answers: Answers, bypassed: string | null) {
  return questions.every((q) => q.id === bypassed || (answers[q.id] !== undefined && answers[q.id] !== ''))
}

function AttemptForm({ data, onSubmit, pending, bypassing }: {
  data: ExamView; onSubmit: (a: Answers) => void; pending: boolean; bypassing: Bypassing
}) {
  const [answers, setAnswers] = useState<Answers>({})
  const questions = data.exam?.questions ?? []
  const [owner = '', name = ''] = data.repo.split('/')
  const chat = { owner, name, pr: String(data.pr), attempt: data.attempt, head_sha: data.head_sha ?? '' }
  const ready = isAnswered(questions, answers, data.bypassed) && !pending
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (ready) onSubmit(Object.fromEntries(Object.entries(answers).filter(([id]) => id !== data.bypassed))) }} className="space-y-4">
      <ol className="space-y-6">
        {questions.map((q) => (
          <Question key={q.id} q={q} bypassing={bypassing} chat={chat} value={answers[q.id]} onChange={(v) => setAnswers((a) => ({ ...a, [q.id]: v }))} />
        ))}
      </ol>
      <button type="submit" disabled={!ready} className="rounded border px-4 py-2 disabled:opacity-50">Submit answers</button>
    </form>
  )
}

type Answering = { changed: boolean; error: string | null; pending: boolean; submit: (a: Answers) => void; bypassing: Bypassing }

/** Submits the open attempt: the answer is the exam as it now reads, grading. A 409 means the exam changed, so it is reloaded. */
function useAnswerExam(owner: string, name: string, pr: string, data: ExamView | undefined): Answering {
  const queryClient = useQueryClient()
  const [changed, setChanged] = useState(false)
  const key = queryKeys.exam(owner, name, pr)
  const submit = useMutation({
    mutationFn: (answers: Answers) => api.answerExam(owner, name, pr, { attempt: data!.attempt, head_sha: data!.head_sha!, answers }),
    onMutate: () => setChanged(false),
    onSuccess: (r) => queryClient.setQueryData<ExamView>(key, r),
    onError: (e) => {
      if (isStatus(e, 409)) {
        setChanged(true)
        void queryClient.invalidateQueries({ queryKey: key })
      }
    },
  })
  const bypass = useMutation({
    mutationFn: ({ on, question }: { on: boolean; question: string }) =>
      api.bypassExam(owner, name, pr, on, { attempt: data!.attempt, head_sha: data!.head_sha!, question }),
    onMutate: () => setChanged(false),
    onSuccess: (r) => queryClient.setQueryData<ExamView>(key, r),
    onError: () => { setChanged(true); void queryClient.invalidateQueries({ queryKey: key }) },   // refused: the exam moved on, so reload it
  })
  const error = submit.isError && !changed ? submit.error.message : null
  const toggle = (question: string) => {
    const on = data!.bypassed !== question
    if (!on || window.confirm('Bypass this question? It will not count toward your score.')) bypass.mutate({ on, question })
  }
  return { changed, error, pending: submit.isPending, submit: (a) => submit.mutate(a), bypassing: { bypassed: data?.bypassed ?? null, toggle } }
}

type ViewProps = { data: ExamView; answer: Answering }

/** The result of the last graded attempt of this head. */
function lastResult(data: ExamView) {
  return data.attempts.findLast((a) => a.result)?.result
}

function LastMisses({ data }: { data: ExamView }) {
  const last = lastResult(data)
  return last && !last.passed ? (
    <section className="space-y-3">
      <p>Score {last.score} / {last.max}{bypassedNote(last)}</p>
      <Misses result={last} />
    </section>
  ) : null
}

/** Which questions of `result` were bypassed, named in the one line that carries the score. */
function bypassedNote(result: ExamResult) {
  const concepts = result.questions.filter((m) => m.help === 'bypassed').map((m) => m.concept)
  return concepts.length ? ` · bypassed: ${concepts.join(', ')}` : ''
}

function GeneratingView({ data }: ViewProps) {
  return <><p>Generating the exam…</p><LastMisses data={data} /></>
}

/** The submitted attempt's questions stay open to chat while it is graded, but take no tip. */
function GradingView({ data }: ViewProps) {
  const [owner = '', name = ''] = data.repo.split('/')
  const grading = data.attempts.findLast((a) => !a.result)
  const chat = { owner, name, pr: String(data.pr), attempt: grading?.attempt ?? data.attempt, head_sha: data.head_sha ?? '' }
  return (
    <>
      <p>Grading your answers…</p>
      <ol className="space-y-6">
        {(grading?.questions ?? []).map((q) => (
          <li key={q.id} className="space-y-2">
            <p className="font-medium">{q.prompt}</p>
            <Assisted q={q} />
            <ExamThread q={q} draft="" target={chat} tips={false} />
          </li>
        ))}
      </ol>
    </>
  )
}

function ErrorView({ data }: ViewProps) {
  return <p className="text-failed-fg">{data.detail}</p>
}

function PassedView({ data }: ViewProps) {
  const last = lastResult(data)
  return last ? (
    <>
      <section className="space-y-1">
        <p className="font-medium">Passed · Score {last.score} / {last.max}{bypassedNote(last)}</p>
        {!data.debt && <p>The exam status is posted as success.</p>}
      </section>
      {/* a passing attempt can still hold partial answers, with the grader's feedback */}
      <section className="space-y-3"><Misses result={last} /></section>
    </>
  ) : null
}

function OpenView({ data, answer }: ViewProps) {
  return (
    <>
      <LastMisses data={data} />
      {data.exam && (
        <AttemptForm key={`${data.attempt}-${data.head_sha}`} data={data} pending={answer.pending} onSubmit={answer.submit} bypassing={answer.bypassing} />
      )}
    </>
  )
}

// what each exam state shows below the notices; any other state shows nothing more
const STATE_VIEWS: Partial<Record<ExamState, (props: ViewProps) => ReactNode>> = {
  generating: GeneratingView, grading: GradingView, error: ErrorView, passed: PassedView, open: OpenView,
}

function Notices({ debt, answer }: { debt: boolean; answer: Answering }) {
  return (
    <>
      {debt && <p className="font-medium">Debt exam</p>}
      {answer.changed && <p role="alert">The exam has changed; it was reloaded.</p>}
      {answer.error && <p role="alert" className="text-failed-fg">{answer.error}</p>}
    </>
  )
}

function ExamBody({ data, answer }: ViewProps) {
  const View = STATE_VIEWS[data.state]
  return (
    <main className="mx-auto max-w-2xl space-y-4 p-4">
      <h1 className="text-lg font-semibold">Exam for {data.repo} #{data.pr}</h1>
      <Notices debt={data.debt} answer={answer} />
      {View && <View data={data} answer={answer} />}
    </main>
  )
}

/** `last` is the exam as the page last read it: a debt exam is deleted once it is passed. */
function ExamError({ error, exam, last }: { error: Error; exam: string; last: ExamView | undefined }) {
  if (!isStatus(error, 404)) return <p className="p-4 text-failed-fg">exam unavailable: {error.message}</p>
  if (last?.debt) return <p className="p-4">The exam debt for {exam} is cleared. Its learning records are kept.</p>
  return <p className="p-4 text-failed-fg">There is no exam for {exam}.</p>
}

export function ExamPage() {
  const { owner = '', name = '', pr = '' } = useParams()
  const exam = useExam(owner, name, pr)
  const answer = useAnswerExam(owner, name, pr, exam.data)
  if (exam.isPending) return <p className="p-4 text-ink-muted">loading exam…</p>
  if (exam.isError) return <ExamError error={exam.error} exam={`${owner}/${name}#${pr}`} last={exam.data} />
  return <ExamBody data={exam.data} answer={answer} />
}
