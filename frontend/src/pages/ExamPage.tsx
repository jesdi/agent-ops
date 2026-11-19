import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router'
import { api, ApiError, type ExamMiss, type ExamQuestion, type ExamResult, type ExamState } from '../lib/api'
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

function Misses({ result }: { result: ExamResult }) {
  const missed = result.questions.filter((m: ExamMiss) => m.points < 1)
  return (
    <section className="space-y-3">
      <p>Score {result.score} / {result.max}</p>
      {missed.map((m) => (
        <article key={m.id} className="space-y-1">
          <h2 className="font-medium">{m.concept}</h2>
          <p>{m.explanation}</p>
          <pre className="overflow-x-auto rounded border border-border bg-surface p-2 text-sm">{m.hunk}</pre>
        </article>
      ))}
    </section>
  )
}

function AttemptForm({ data, onSubmit, pending }: { data: ExamState; onSubmit: (a: Answers) => void; pending: boolean }) {
  const [answers, setAnswers] = useState<Answers>({})
  const questions = data.exam?.questions ?? []
  const complete = questions.every((q) => answers[q.id] !== undefined && answers[q.id] !== '')
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (complete && !pending) onSubmit(answers) }} className="space-y-4">
      <ol className="space-y-6">
        {questions.map((q) => (
          <Question key={q.id} q={q} value={answers[q.id]} onChange={(v) => setAnswers((a) => ({ ...a, [q.id]: v }))} />
        ))}
      </ol>
      <button type="submit" disabled={!complete || pending} className="rounded border px-4 py-2 disabled:opacity-50">Submit answers</button>
    </form>
  )
}

export function ExamPage() {
  const { owner = '', name = '', pr = '' } = useParams()
  const queryClient = useQueryClient()
  const exam = useExam(owner, name, pr)
  const [changed, setChanged] = useState(false)
  const key = queryKeys.exam(owner, name, pr)
  const submit = useMutation({
    mutationFn: (answers: Answers) => api.answerExam(owner, name, pr, { attempt: exam.data!.attempt, head_sha: exam.data!.head_sha!, answers }),
    onMutate: () => setChanged(false),
    onSuccess: (r) => queryClient.setQueryData<ExamState>(key, (prev) => prev && { ...prev, state: r.state ?? 'grading', exam: null }),
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        setChanged(true)
        void queryClient.invalidateQueries({ queryKey: key })
      }
    },
  })

  if (exam.isPending) return <p className="p-4 text-ink-muted">loading exam…</p>
  if (exam.isError) {
    const missing = exam.error instanceof ApiError && exam.error.status === 404
    return <p className="p-4 text-failed-fg">{missing ? `There is no exam for ${owner}/${name}#${pr}.` : `exam unavailable: ${exam.error.message}`}</p>
  }
  const data = exam.data
  const last = data.results.at(-1)
  const failedError = submit.isError && !changed ? submit.error.message : null

  return (
    <main className="mx-auto max-w-2xl space-y-4 p-4">
      <h1 className="text-lg font-semibold">Exam for {data.repo} #{data.pr}</h1>
      {data.debt && <p className="font-medium">Debt exam</p>}
      {changed && <p role="alert">The exam has changed; it was reloaded.</p>}
      {failedError && <p role="alert" className="text-failed-fg">{failedError}</p>}
      {data.state === 'generating' && <p>Generating the exam…</p>}
      {data.state === 'grading' && <p>Grading your answers…</p>}
      {data.state === 'error' && <p className="text-failed-fg">{data.detail}</p>}
      {data.state === 'passed' && last && (
        <section className="space-y-1">
          <h2 className="font-medium">Passed</h2>
          <p>Score {last.score} / {last.max}</p>
          <p>The exam status is posted as success.</p>
        </section>
      )}
      {(data.state === 'open' || data.state === 'generating') && last && !last.passed && <Misses result={last} />}
      {data.state === 'open' && data.exam && (
        <AttemptForm key={`${data.attempt}-${data.head_sha}`} data={data} pending={submit.isPending} onSubmit={(a) => submit.mutate(a)} />
      )}
    </main>
  )
}
