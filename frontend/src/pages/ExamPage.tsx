import { useEffect, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router'
import { api, ApiError, type ExamAttempt, type ExamQuestion, type ExamResult, type ExamState, type ExamView } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'
import { useExam } from '../hooks/useResources'
import { chip } from '../lib/tone'
import { CODE, ExamThread, GeneralChat, QuoteContext, type ChatTarget, type GeneralThread, type Quote } from '../components/ExamThread'

type Answers = Record<string, number | string>

type Bypassing = { bypassed: string | null; toggle: (question: string) => void }

type ExamResultQuestion = ExamResult['questions'][number]

type Help = ExamResultQuestion['help']

/** The chat target of attempt `attempt` of the exam. */
function chatTarget(data: ExamView, attempt: number): ChatTarget {
  const [owner = '', name = ''] = data.repo.split('/')
  return { owner, name, pr: String(data.pr), attempt, head_sha: data.head_sha ?? '' }
}

/** Marks an element whose selected text quotes that question of that attempt into its thread. */
const quotes = (attempt: number, question: string) => ({ 'data-quote-attempt': attempt, 'data-quote-question': question })

/** The help a question had, as a badge: assisted, or bypassed. */
function HelpBadge({ help }: { help: Help | undefined }) {
  return help ? <span className={`ml-2 align-middle ${help === 'bypassed' ? chip.parked : chip.waiting}`}>{help}</span> : null
}

/** A question's prompt, as the title of its item, with the help it had. */
function Prompt({ q, help, id }: { q: ExamQuestion; help: Help | undefined; id?: string }) {
  return <p id={id} className="font-medium">{q.prompt}<HelpBadge help={help} /></p>
}

/** An open question: its answer, its actions (Tip, Ask, Bypass) in one row, and its thread. */
function Question({ q, value, onChange, bypassing, chat, tipCap, takes }: {
  q: ExamQuestion; value: number | string | undefined; onChange: (v: number | string) => void; bypassing: Bypassing
  chat: ChatTarget; tipCap: number; takes: { chat: boolean; bypass: boolean }
}) {
  const promptId = `prompt-${q.id}`
  const bypassed = bypassing.bypassed === q.id
  const draft = typeof value === 'string' ? value : ''
  return (
    <li className="space-y-2" {...quotes(chat.attempt, q.id)}>
      <Prompt id={promptId} q={q} help={bypassed ? 'bypassed' : openHelp(q)} />
      <Answer q={q} promptId={promptId} value={value} draft={draft} onChange={onChange} />
      <ExamThread q={q} draft={draft} target={chat} tipCap={tipCap} chat={takes.chat}>
        {takes.bypass && <BypassButton bypassed={bypassed} onToggle={() => bypassing.toggle(q.id)} />}
      </ExamThread>
    </li>
  )
}

/** The help an unsubmitted question has had so far. */
const openHelp = (q: ExamQuestion): Help => (q.assisted ? 'assisted' : null)

function Answer({ q, promptId, value, draft, onChange }: {
  q: ExamQuestion; promptId: string; value: number | string | undefined; draft: string; onChange: (v: number | string) => void
}) {
  if (q.type !== 'choice') {
    return (
      <textarea aria-labelledby={promptId} rows={4} value={draft} onChange={(e) => onChange(e.target.value)}
        className="w-full rounded border border-ink-muted bg-transparent p-2" />
    )
  }
  return (
    <div role="radiogroup" aria-labelledby={promptId} className="space-y-1">
      {(q.options ?? []).map((option, i) => (
        <label key={option} className="flex items-center gap-2 py-1">
          <input type="radio" name={q.id} checked={value === i} onChange={() => onChange(i)} />
          {option}
        </label>
      ))}
    </div>
  )
}

function BypassButton({ bypassed, onToggle }: { bypassed: boolean; onToggle: () => void }) {
  return (
    <button type="button" onClick={onToggle} className="rounded border px-2 py-1 text-sm">
      {bypassed ? 'Undo bypass' : 'Bypass'}
    </button>
  )
}

/** A graded question's explanation, the grader's feedback under it, and its hunk: each only where the result has it. */
function Explained({ r }: { r: ExamResultQuestion }) {
  return (
    <>
      {r.explanation && <p>{r.explanation}</p>}
      {r.feedback && <p className="text-sm">{r.feedback}</p>}
      {r.hunk && <pre className={CODE}>{r.hunk}</pre>}
    </>
  )
}

/** The score line of a graded attempt: the verdict, the points, and the bypassed questions left out of them. */
function scoreLine(result: ExamResult) {
  const bypassed = result.questions.filter((m) => m.help === 'bypassed').map((m) => m.concept)
  const note = bypassed.length ? [`bypassed: ${bypassed.join(', ')}`] : []
  return [result.passed ? 'Passed' : 'Failed', `Score ${result.score} / ${result.max}`, ...note].join(' · ')
}

/** A submitted attempt: each question with its outcome line (its points once graded) and its explanation, then its thread. */
function AttemptReview({ data, kept }: { data: ExamView; kept: ExamAttempt }) {
  const results = new Map((kept.result?.questions ?? []).map((r) => [r.id, r]))
  const chat = chatTarget(data, kept.attempt)
  return (
    <section className="space-y-3">
      <h2 className="font-medium">Attempt {kept.attempt}{kept.result && ` · ${scoreLine(kept.result)}`}</h2>
      <ol className="space-y-6">
        {kept.questions.map((q) => (
          <KeptQuestion key={q.id} q={q} r={results.get(q.id)} chat={chat} tipCap={data.tip_cap} chats={kept.chat} />
        ))}
      </ol>
    </section>
  )
}

/** A submitted question: its help (the flags sealed at submit once graded), its points, its explanation, and its thread. */
function KeptQuestion({ q, r, chat, tipCap, chats }: {
  q: ExamQuestion; r: ExamResultQuestion | undefined; chat: ChatTarget; tipCap: number; chats: boolean
}) {
  return (
    <li className="space-y-2" {...quotes(chat.attempt, q.id)}>
      <Prompt q={q} help={r ? r.help : openHelp(q)} />
      {r && <Outcome r={r} />}
      <ExamThread q={q} draft="" target={chat} tipCap={tipCap} chat={chats} />
    </li>
  )
}

/** A graded question's points (none for a bypassed one), then its explanation. */
function Outcome({ r }: { r: ExamResultQuestion }) {
  return (
    <>
      {r.points != null && <p className="text-sm text-ink-muted">{r.points} / 1</p>}
      <Explained r={r} />
    </>
  )
}

/** Every submitted attempt of this head, oldest first. */
function Attempts({ data }: { data: ExamView }) {
  return data.attempts.map((a) => <AttemptReview key={a.attempt} data={data} kept={a} />)
}

function isStatus(error: unknown, status: number) {
  return error instanceof ApiError && error.status === status
}

function isAnswered(questions: ExamQuestion[], answers: Answers, bypassed: string | null) {
  return questions.every((q) => q.id === bypassed || (answers[q.id] !== undefined && answers[q.id] !== ''))
}

type OpenExam = NonNullable<ExamView['exam']>

function AttemptForm({ data, exam, onSubmit, pending, bypassing }: {
  data: ExamView; exam: OpenExam; onSubmit: (a: Answers) => void; pending: boolean; bypassing: Bypassing
}) {
  const [answers, setAnswers] = useState<Answers>({})
  const chat = chatTarget(data, data.attempt)
  const ready = isAnswered(exam.questions, answers, data.bypassed) && !pending
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (ready) onSubmit(Object.fromEntries(Object.entries(answers).filter(([id]) => id !== data.bypassed))) }} className="space-y-4">
      <ol className="space-y-6">
        {exam.questions.map((q) => (
          <Question key={q.id} q={q} bypassing={bypassing} chat={chat} tipCap={data.tip_cap} takes={exam}
            value={answers[q.id]} onChange={(v) => setAnswers((a) => ({ ...a, [q.id]: v }))} />
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

function GeneratingView({ data }: ViewProps) {
  return <><p>Generating the exam…</p><Attempts data={data} /></>
}

/** The submitted attempt's questions stay open to chat while it is graded, but take no tip. */
function GradingView({ data }: ViewProps) {
  return <><p>Grading your answers…</p><Attempts data={data} /></>
}

function ErrorView({ data }: ViewProps) {
  return <p className="text-failed-fg">{data.detail}</p>
}

function PassedView({ data }: ViewProps) {
  return (
    <>
      {!data.debt && <p>The exam status is posted as success.</p>}
      {/* a passing attempt can still hold partial answers, with the grader's feedback */}
      <Attempts data={data} />
    </>
  )
}

function OpenView({ data, answer }: ViewProps) {
  return (
    <>
      <Attempts data={data} />
      {data.exam && (
        <AttemptForm key={`${data.attempt}-${data.head_sha}`} data={data} exam={data.exam} pending={answer.pending}
          onSubmit={answer.submit} bypassing={answer.bypassing} />
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

/** Each attempt's general chat the page shows, oldest first: those that take a message now, and those that have one. */
function generalChats(data: ExamView): GeneralThread[] {
  const all = [...data.attempts, ...(data.exam ? [{ ...data.exam, attempt: data.attempt }] : [])]
  return all.filter((a) => a.chat || a.general.length > 0)
    .map((a) => ({ target: chatTarget(data, a.attempt), thread: a.general, chat: a.chat }))
}

/** The quote of the operator's last selection: inside a question or its result it is for that question's thread, elsewhere on
 *  the page for the general chat of `attempt`. A selection inside a text box or the general chat quotes nothing. */
function selectedQuote(attempt: number): Quote | null {
  const selection = document.getSelection()
  const text = selection?.toString().trim()
  const at = selectedElement(selection?.anchorNode)
  if (!text || !at || at.closest('textarea, input, [data-chat]')) return null
  return quoteIn(at.closest<HTMLElement>('[data-quote-question]'), attempt, text)
}

/** A quote for the question `holder` marks, or for the general chat of `attempt` when none does. */
const quoteIn = (holder: HTMLElement | null, attempt: number, text: string): Quote =>
  holder ? { attempt: Number(holder.dataset.quoteAttempt), question: holder.dataset.quoteQuestion!, text } : { attempt, question: null, text }

const selectedElement = (node: Node | null | undefined) => (node instanceof Element ? node : node?.parentElement)

/** The page's last selection as a quote; one outside every question is for the general chat of `attempt`. */
function useSelectionQuote(attempt: number | undefined) {
  const [quote, setQuote] = useState<Quote | null>(null)
  useEffect(() => {
    if (attempt === undefined) return
    const onSelect = () => {
      const quote = selectedQuote(attempt)
      if (quote) setQuote(quote)
    }
    document.addEventListener('selectionchange', onSelect)
    return () => document.removeEventListener('selectionchange', onSelect)
  }, [attempt])
  return quote
}

function ExamBody({ data, answer }: ViewProps) {
  const View = STATE_VIEWS[data.state]
  const chats = generalChats(data)
  const quote = useSelectionQuote(chats.findLast((c) => c.chat)?.target.attempt)
  const [chatOpen, setChatOpen] = useState(false)
  return (
    <QuoteContext.Provider value={quote}>
      {/* on a wide screen an open drawer takes its width from the page, so nothing hides behind it */}
      <div className={chatOpen ? 'md:pr-96' : undefined}>
        <main className="mx-auto w-full max-w-2xl space-y-4 p-4 pb-20">
          <h1 className="text-lg font-semibold">Exam for {data.repo} #{data.pr}</h1>
          <Notices debt={data.debt} answer={answer} />
          {View && <View data={data} answer={answer} />}
        </main>
      </div>
      {chats.length > 0 && <GeneralChat chats={chats} open={chatOpen} setOpen={setChatOpen} />}
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
