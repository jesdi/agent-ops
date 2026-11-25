import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type ExamQuestion, type ExamTurn, type TurnBody, type TurnEnd } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'

/** Which exam attempt a chat belongs to. */
export type ChatTarget = { owner: string; name: string; pr: string; attempt: number; head_sha: string }

/** Text the operator selected, for the thread of `question` of `attempt`, or the general chat (null). */
export type Quote = { attempt: number; question: string | null; text: string }

/** The page's last selection; each chat takes the one meant for it. */
export const QuoteContext = createContext<Quote | null>(null)

/** One attempt's general chat: its turns, and whether it takes a message now. */
export type GeneralThread = { target: ChatTarget; thread: ExamTurn[]; chat: boolean }

type Live = { id: string; kind: ExamTurn['kind']; message: string; text: string; error: string | null }

/** Code in a reply or a result: the console's monospace, wrapped so a long line never widens the page. */
export const CODE = 'whitespace-pre-wrap wrap-anywhere rounded border border-border bg-surface p-2 font-mono text-xs'

/** The quote meant for this chat, or null. */
function useQuoteFor(attempt: number, question: string | null) {
  const quote = useContext(QuoteContext)
  return quote && quote.attempt === attempt && quote.question === question ? quote : null
}

/** One thread's turns: it starts them, and it alone watches the reply of the one running, whether this page started it or
 *  a reload found it running. A turn runs on the console, so the stored thread, read again once the turn ends, is what stays. */
function useChat(target: ChatTarget, question: string | null, thread: ExamTurn[]) {
  const queryClient = useQueryClient()
  const [live, setLive] = useState<Live | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const watched = useRef<string | null>(null)
  const { owner, name, pr } = target

  const watch = useCallback(async (seed: Live) => {
    watched.current = seed.id
    setLive(seed)
    const mine = (change: (l: Live) => Live | null) => setLive((l) => (l?.id === seed.id ? change(l) : l))
    const end = await api.followTurn(owner, name, pr, seed.id, (delta) => mine((l) => ({ ...l, text: l.text + delta })))
      .catch((e: Error): TurnEnd => ({ status: 'failed', error: e.message }))
    if (end.status === 'failed') mine((l) => ({ ...l, error: end.error }))
    await queryClient.invalidateQueries({ queryKey: queryKeys.exam(owner, name, pr) })
    if (end.status === 'done') mine(() => null)
  }, [owner, name, pr, queryClient])

  const running = thread.find((t) => t.status === 'running')
  useEffect(() => {
    if (running && watched.current !== running.id) void watch({ id: running.id, kind: running.kind, message: running.message, text: '', error: null })
  }, [running, watch])

  /** Starts a turn; true once the console took it. */
  const run = async (kind: TurnBody['kind'], message: string) => {
    setRefusal(null)
    try {
      const { id } = await api.startTurn(owner, name, pr, { attempt: target.attempt, head_sha: target.head_sha, kind, question, message } as TurnBody)
      void watch({ id, kind, message, text: '', error: null })
      return true
    } catch (e) {
      setRefusal((e as Error).message)
      return false
    }
  }
  return { live, refusal, busy: isBusy(live, running), run }
}

/** A turn is running here: one this page watches, or one the stored thread still shows running. */
const isBusy = (live: Live | null, running: ExamTurn | undefined) => (live !== null && live.error === null) || running !== undefined

/** A message box whose draft survives closing it and a refused send, and takes a quote when one is meant for it. */
function useDraft(quote: Quote | null, onQuote: () => void) {
  const [message, setMessage] = useState('')
  const [seen, setSeen] = useState(quote)
  if (quote !== seen) {
    setSeen(quote)
    if (quote) {
      setMessage(`> ${quote.text}\n\n`)
      onQuote()
    }
  }
  return [message, setMessage] as const
}

const FENCE = /```[^\n]*\n?([\s\S]*?)(?:```|$)/

type Block = { code: boolean; text: string }

/** A reply's blocks: each fenced code block, and each paragraph of the prose around them, a bare diff counting as code. */
function blocksOf(text: string): Block[] {
  return text.split(FENCE).flatMap((part, i) => (i % 2   // prose, code, prose, code, …
    ? [{ code: true, text: part.replace(/\n+$/, '') }]
    : part.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean).map((p) => ({ code: isDiff(p), text: p }))))
}

/** A paragraph that is a diff left unfenced: every line a diff line. */
const isDiff = (text: string) => text.split('\n').every((line) => /^(@@|\+|-|diff --git)/.test(line))

/** A reply's text: its prose as written, its code in the console's monospace. */
function ReplyText({ text }: { text: string }) {
  return blocksOf(text).map((b, i) => (b.code
    ? <pre key={i} className={CODE}>{b.text}</pre>
    : <p key={i} className="whitespace-pre-wrap">{b.text}</p>))
}

function Turn({ turn, onRetry }: { turn: Pick<ExamTurn, 'kind' | 'message' | 'text'> & { error: string | null }; onRetry?: () => void }) {
  return (
    <div className="space-y-1 rounded border border-border p-2 text-sm wrap-anywhere">
      {turn.kind !== 'tip' && <p className="whitespace-pre-wrap text-ink-muted">{turn.message}</p>}
      <ReplyText text={turn.text} />
      {turn.error && (
        <p role="alert" className="text-failed-fg">
          {turn.error} {onRetry && <button type="button" onClick={onRetry} className="rounded border px-2 py-0.5">Retry</button>}
        </p>
      )}
    </div>
  )
}

const ACTION = 'rounded border px-2 py-1 text-sm disabled:opacity-50'

/** A question's actions in one row (Tip n / cap while the attempt takes tips, Ask while it chats, then `children`) with its
 *  thread inline under them. */
export function ExamThread({ q, draft, target, tipCap, chat, children }: {
  q: ExamQuestion; draft: string; target: ChatTarget; tipCap: number; chat: boolean; children?: ReactNode
}) {
  const thread = useChat(target, q.id, q.thread)
  const [asking, setAsking] = useState(false)
  const [message, setMessage] = useDraft(useQuoteFor(target.attempt, q.id), () => setAsking(true))
  const send = async () => { if (await thread.run('question', message)) setMessage('') }
  const retry = chat ? (kind: ExamTurn['kind'], text: string) => void thread.run(kind, text) : undefined

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {q.tips_left !== null && (
          <button type="button" disabled={thread.busy || q.tips_left === 0} onClick={() => void thread.run('tip', draft)} className={ACTION}>
            Tip {q.tips} / {tipCap}
          </button>
        )}
        {chat && <button type="button" aria-expanded={asking} onClick={() => setAsking((a) => !a)} className={ACTION}>Ask</button>}
        {children}
      </div>
      {chat && asking && <AskBox label="Message about this question" message={message} onChange={setMessage} busy={thread.busy} onSend={() => void send()} />}
      {thread.refusal && <p role="alert" className="text-failed-fg">{thread.refusal}</p>}
      <Turns thread={q.thread} live={thread.live} onRetry={retry} />
    </div>
  )
}

/** The general chats, in a drawer on the right, a bottom sheet below `md`: one section per attempt, oldest first, each
 *  taking a message while its attempt chats. It stays mounted while closed, so a running reply is still followed. */
export function GeneralChat({ chats, open, setOpen }: { chats: GeneralThread[]; open: boolean; setOpen: (open: boolean) => void }) {
  return (
    <>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
        className="fixed bottom-4 right-4 z-10 rounded border border-border bg-surface px-3 py-2 shadow">
        Chat
      </button>
      <aside data-chat aria-label="General chat" hidden={!open}
        className="fixed inset-x-0 bottom-0 z-20 max-h-[70vh] space-y-4 overflow-y-auto border-t border-border bg-surface-raised p-4 shadow md:inset-y-0 md:left-auto md:max-h-none md:w-96 md:border-l md:border-t-0">
        <div className="flex items-center justify-between">
          <h2 className="font-medium">General chat</h2>
          <button type="button" onClick={() => setOpen(false)} className={ACTION}>Close</button>
        </div>
        {chats.map((c) => <GeneralSection key={c.target.attempt} {...c} titled={chats.length > 1} onQuote={() => setOpen(true)} />)}
      </aside>
    </>
  )
}

function GeneralSection({ target, thread, chat, titled, onQuote }: GeneralThread & { titled: boolean; onQuote: () => void }) {
  const general = useChat(target, null, thread)
  const [message, setMessage] = useDraft(useQuoteFor(target.attempt, null), onQuote)
  const send = async () => { if (await general.run('general', message)) setMessage('') }
  const retry = chat ? (kind: ExamTurn['kind'], text: string) => void general.run(kind, text) : undefined
  return (
    <section className="space-y-2">
      {titled && <h3 className="text-sm text-ink-muted">Attempt {target.attempt}</h3>}
      <Turns thread={thread} live={general.live} onRetry={retry} />
      {general.refusal && <p role="alert" className="text-failed-fg">{general.refusal}</p>}
      {chat && <AskBox label={titled ? `Message to the general chat of attempt ${target.attempt}` : 'Message to the general chat'}
        message={message} onChange={setMessage} busy={general.busy} onSend={() => void send()} />}
    </section>
  )
}

function AskBox({ label, message, onChange, busy, onSend }: {
  label: string; message: string; onChange: (m: string) => void; busy: boolean; onSend: () => void
}) {
  return (
    <div className="flex gap-2">
      <textarea aria-label={label} rows={2} value={message}
        onChange={(e) => onChange(e.target.value)} className="min-w-0 flex-1 rounded border border-ink-muted bg-transparent p-2" />
      <button type="button" disabled={busy || !message.trim()} onClick={onSend} className={`self-start ${ACTION}`}>Send</button>
    </div>
  )
}

/** The stored thread, then the reply as it streams until the stored turn has it. */
function Turns({ thread, live, onRetry }: { thread: ExamTurn[]; live: Live | null; onRetry?: (kind: ExamTurn['kind'], text: string) => void }) {
  const showLive = live && !thread.some((t) => t.id === live.id)
  const retry = (t: Pick<ExamTurn, 'kind' | 'message'>) => (onRetry ? () => onRetry(t.kind, t.message) : undefined)
  return (
    <>
      {thread.map((t) => (
        <Turn key={t.id} turn={t.id === live?.id && t.status === 'running' ? { ...t, text: live.text, error: live.error } : t} onRetry={retry(t)} />
      ))}
      {showLive && <Turn turn={live} onRetry={retry(live)} />}
    </>
  )
}
