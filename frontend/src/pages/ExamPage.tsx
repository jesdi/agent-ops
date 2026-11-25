import { useEffect, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router'
import { api, ApiError, type ExamAttempt, type ExamQuestion, type ExamResult, type ExamState, type ExamView } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'
import { useExam } from '../hooks/useResources'
import { ExamThread, GeneralChat, QuoteContext, type ChatTarget, type Quote } from '../components/ExamThread'

type Answers = Record<string, number | string>

type Bypassing = { bypassed: string | null; toggle: (question: string) => void }

type ExamResultQuestion = ExamResult['questions'][number]

/** The chat target of attempt `attempt` of the exam. */
function chatTarget(data: ExamView, attempt: number): ChatTarget {
  const [owner = '', name = ''] = data.repo.split('/')
  return { owner, name, pr: String(data.pr), attempt, head_sha: data.head_sha ?? '' }
}

/** Marks an element whose selected text quotes that question of that attempt into its thread. */
const quotes = (attempt: number, question: string) => ({ 'data-quote-attempt': attempt, 'data-quote-question': question })

function Question({ q, value, onChange, bypassing, chat }: {
  q: ExamQuestion; value: number | string | undefined; onChange: (v: number | string) => void; bypassing: Bypassing; chat: ChatTarget
}) {
  const promptId = `prompt-${q.id}`
  const bypassed = bypassing.bypassed === q.id
  const draft = typeof value === 'string' ? value : ''
  return (
    <li className="space-y-2" {...quotes(chat.attempt, q.id)}>
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

/** The questions of `result` that lost points or had help, each with its explanation, the grader's feedback and its hunk. */
function Misses({ result }: { result: ExamResult }) {
  return result.questions.filter((m) => m.help || (m.points ?? 1) < 1).map((m) => (
    <article key={m.id} className="space-y-1">
      <h2 className="font-medium">{m.concept}</h2>
      {m.help === 'assisted' && <p className="text-sm text-ink-muted">assisted</p>}
      <Explained r={m} />
    </article>
  ))
}

/** A graded question's explanation, the grader's feedback under it, and its hunk: each only where the result has it. */
function Explained({ r }: { r: ExamResultQuestion }) {
  return (
    <>
      {r.explanation && <p>{r.explanation}</p>}
      {r.feedback && <p className="text-sm">{r.feedback}</p>}
      {r.hunk && <pre className="overflow-x-auto rounded border border-border bg-surface p-2 text-sm">{r.hunk}</pre>}
    </>
  )
}

/** A kept question's outcome: its points and help once graded (the flags sealed at submit), its assisted mark while grading. */
function Outcome({ q, r }: { q: ExamQuestion; r: ExamResultQuestion | undefined }) {
  const help = r ? r.help : q.assisted ? 'assisted' : null
  const line = [r?.points != null ? `${r.points} / 1` : null, help].filter(Boolean).join(' · ')
  return (
    <>
      {line && <p className="text-sm text-ink-muted">{line}</p>}
      {r && <Explained r={r} />}
    </>
  )
}

/** A submitted attempt with its questions, each with its outcome and its thread, open to chat but taking no tip. */
function AttemptReview({ data, kept }: { data: ExamView; kept: ExamAttempt }) {
  const results = new Map((kept.result?.questions ?? []).map((r) => [r.id, r]))
  const chat = chatTarget(data, kept.attempt)
  const score = kept.result && !kept.result.passed ? ` · Score ${kept.result.score} / ${kept.result.max}` : ''
  return (
    <section className="space-y-3">
      <h2 className="font-medium">Attempt {kept.attempt}{score}</h2>
      <ol className="space-y-6">
        {kept.questions.map((q) => (
          <li key={q.id} className="space-y-2" {...quotes(kept.attempt, q.id)}>
            <p className="font-medium">{q.prompt}</p>
            <Outcome q={q} r={results.get(q.id)} />
            <ExamThread q={q} draft="" target={chat} tips={false} />
          </li>
        ))}
      </ol>
    </section>
  )
}

/** Every submitted attempt of this head, oldest first. One kept from before attempts had their own questions shows only
 *  its misses, and only while it is the last. */
function Kept({ data }: { data: ExamView }) {
  const last = data.attempts.at(-1)
  return data.attempts.map((a) => a.questions.length
    ? <AttemptReview key={a.attempt} data={data} kept={a} />
    : a === last && a.result ? <Summary key={a.attempt} result={a.result} /> : null)
}

function Summary({ result }: { result: ExamResult }) {
  return (
    <section className="space-y-3">
      {!result.passed && <p>Score {result.score} / {result.max}{bypassedNote(result)}</p>}
      <Misses result={result} />
    </section>
  )
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
  const chat = chatTarget(data, data.attempt)
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

/** Which questions of `result` were bypassed, named in the one line that carries the score. */
function bypassedNote(result: ExamResult) {
  const concepts = result.questions.filter((m) => m.help === 'bypassed').map((m) => m.concept)
  return concepts.length ? ` · bypassed: ${concepts.join(', ')}` : ''
}

function GeneratingView({ data }: ViewProps) {
  return <><p>Generating the exam…</p><Kept data={data} /></>
}

/** The submitted attempt's questions stay open to chat while it is graded, but take no tip. */
function GradingView({ data }: ViewProps) {
  return <><p>Grading your answers…</p><Kept data={data} /></>
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
      <Kept data={data} />
    </>
  ) : null
}

function OpenView({ data, answer }: ViewProps) {
  return (
    <>
      <Kept data={data} />
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

/** The attempt the general chat talks about: the open one, else the last submitted; none before the first opens. */
function chattingAttempt(data: ExamView) {
  return data.exam ? data.attempt : data.attempts.at(-1)?.attempt
}

const CHATTING: ExamState[] = ['generating', 'open', 'grading', 'passed']

/** The general chat of the latest attempt, with every earlier attempt's kept above it. */
function Chat({ data }: { data: ExamView }) {
  const attempt = chattingAttempt(data)
  if (attempt === undefined || !CHATTING.includes(data.state)) return null
  const earlier = data.attempts.filter((a) => a.attempt !== attempt).map((a) => ({ attempt: a.attempt, thread: a.general ?? [] }))
  const thread = (data.exam ? data.exam.general : data.attempts.at(-1)?.general) ?? []
  return <GeneralChat key={`${attempt}-${data.head_sha}`} target={chatTarget(data, attempt)} thread={thread} earlier={earlier} />
}

/** The quote of the operator's last selection: inside a question or its result it is for that question's thread, elsewhere on
 *  the page for the general chat. A selection inside a text box or the general chat quotes nothing. */
function useSelectionQuote(attempt: number | undefined) {
  const [quote, setQuote] = useState<Quote | null>(null)
  useEffect(() => {
    if (attempt === undefined) return
    const onSelect = () => {
      const selection = document.getSelection()
      const text = selection?.toString().trim()
      const node = selection?.anchorNode
      const at = node instanceof Element ? node : node?.parentElement
      if (!text || !at || at.closest('textarea, input, [data-chat]')) return
      const holder = at.closest<HTMLElement>('[data-quote-question]')
      setQuote(holder
        ? { attempt: Number(holder.dataset.quoteAttempt), question: holder.dataset.quoteQuestion ?? null, text }
        : { attempt, question: null, text })
    }
    document.addEventListener('selectionchange', onSelect)
    return () => document.removeEventListener('selectionchange', onSelect)
  }, [attempt])
  return quote
}

function ExamBody({ data, answer }: ViewProps) {
  const View = STATE_VIEWS[data.state]
  const quote = useSelectionQuote(chattingAttempt(data))
  return (
    <QuoteContext.Provider value={quote}>
      <main className="mx-auto max-w-2xl space-y-4 p-4 pb-20">
        <h1 className="text-lg font-semibold">Exam for {data.repo} #{data.pr}</h1>
        <Notices debt={data.debt} answer={answer} />
        {View && <View data={data} answer={answer} />}
        <Chat data={data} />
      </main>
    </QuoteContext.Provider>
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
