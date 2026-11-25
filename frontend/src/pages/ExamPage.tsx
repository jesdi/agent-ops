import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router'
import { api, ApiError, type ExamQuestion, type ExamResult, type ExamState, type ExamView } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'
import { useExam } from '../hooks/useResources'

type Answers = Record<string, number | string>

function Question({ q, value, onChange }: { q: ExamQuestion; value: number | string | undefined; onChange: (v: number | string) => void }) {
  const promptId = `prompt-${q.id}`
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
          value={typeof value === 'string' ? value : ''}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded border border-ink-muted bg-transparent p-2"
        />
      )}
    </li>
  )
}

function Misses({ result, showScore = true }: { result: ExamResult; showScore?: boolean }) {
  const missed = result.questions.filter((m) => m.points < 1)
  return (
    <section className="space-y-3">
      {showScore && <p>Score {result.score} / {result.max}</p>}
      {missed.map((m) => (
        <article key={m.id} className="space-y-1">
          <h2 className="font-medium">{m.concept}</h2>
          <p>{m.explanation}</p>
          {m.feedback && <p className="text-sm">{m.feedback}</p>}
          <pre className="overflow-x-auto rounded border border-border bg-surface p-2 text-sm">{m.hunk}</pre>
        </article>
      ))}
    </section>
  )
}

function isStatus(error: unknown, status: number) {
  return error instanceof ApiError && error.status === status
}

function isAnswered(questions: ExamQuestion[], answers: Answers) {
  return questions.every((q) => answers[q.id] !== undefined && answers[q.id] !== '')
}

function AttemptForm({ data, onSubmit, pending }: { data: ExamView; onSubmit: (a: Answers) => void; pending: boolean }) {
  const [answers, setAnswers] = useState<Answers>({})
  const questions = data.exam?.questions ?? []
  const ready = isAnswered(questions, answers) && !pending
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (ready) onSubmit(answers) }} className="space-y-4">
      <ol className="space-y-6">
        {questions.map((q) => (
          <Question key={q.id} q={q} value={answers[q.id]} onChange={(v) => setAnswers((a) => ({ ...a, [q.id]: v }))} />
        ))}
      </ol>
      <button type="submit" disabled={!ready} className="rounded border px-4 py-2 disabled:opacity-50">Submit answers</button>
    </form>
  )
}

type Answering = { changed: boolean; error: string | null; pending: boolean; submit: (a: Answers) => void }

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
  const error = submit.isError && !changed ? submit.error.message : null
  return { changed, error, pending: submit.isPending, submit: (a) => submit.mutate(a) }
}

type ViewProps = { data: ExamView; answer: Answering }

/** The result of the last graded attempt of this head. */
function lastResult(data: ExamView) {
  return data.attempts.findLast((a) => a.result)?.result
}

function LastMisses({ data }: { data: ExamView }) {
  const last = lastResult(data)
  return last && !last.passed ? <Misses result={last} /> : null
}

function GeneratingView({ data }: ViewProps) {
  return <><p>Generating the exam…</p><LastMisses data={data} /></>
}

function GradingView() {
  return <p>Grading your answers…</p>
}

function ErrorView({ data }: ViewProps) {
  return <p className="text-failed-fg">{data.detail}</p>
}

function PassedView({ data }: ViewProps) {
  const last = lastResult(data)
  return last ? (
    <>
      <section className="space-y-1">
        <h2 className="font-medium">Passed</h2>
        <p>Score {last.score} / {last.max}</p>
        {!data.debt && <p>The exam status is posted as success.</p>}
      </section>
      {/* a passing attempt can still hold partial answers, with the grader's feedback */}
      <Misses result={last} showScore={false} />
    </>
  ) : null
}

function OpenView({ data, answer }: ViewProps) {
  return (
    <>
      <LastMisses data={data} />
      {data.exam && (
        <AttemptForm key={`${data.attempt}-${data.head_sha}`} data={data} pending={answer.pending} onSubmit={answer.submit} />
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
