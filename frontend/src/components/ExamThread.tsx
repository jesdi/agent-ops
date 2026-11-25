import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type ExamQuestion, type ExamTurn, type TurnBody, type TurnEnd } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'

/** Which exam attempt a chat belongs to. */
export type ChatTarget = { owner: string; name: string; pr: string; attempt: number; head_sha: string }

/** Text the operator selected, for the thread of `question` of `attempt`, or the general chat (null). */
export type Quote = { attempt: number; question: string | null; text: string }

/** The page's last selection; each chat takes the one meant for it. */
export const QuoteContext = createContext<Quote | null>(null)

const TIP_CAP = 3

type Live = { id: string; kind: ExamTurn['kind']; message: string; text: string; error: string | null }

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
      const { id } = await api.startTurn(owner, name, pr, { attempt: target.attempt, head_sha: target.head_sha, kind, question, message })
      void watch({ id, kind, message, text: '', error: null })
      return true
    } catch (e) {
      setRefusal((e as Error).message)
      return false
    }
  }
  const busy = live !== null && live.error === null || running !== undefined
  return { live, refusal, busy, run }
}

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

function Turn({ turn, onRetry }: { turn: Pick<ExamTurn, 'kind' | 'message' | 'text'> & { error: string | null }; onRetry?: () => void }) {
  return (
    <div className="space-y-1 rounded border border-border p-2 text-sm">
      {turn.kind !== 'tip' && <p className="text-ink-muted">{turn.message}</p>}
      {turn.text && <p className="whitespace-pre-wrap">{turn.text}</p>}
      {turn.error && (
        <p role="alert" className="text-failed-fg">
          {turn.error} {onRetry && <button type="button" onClick={onRetry} className="rounded border px-2 py-0.5">Retry</button>}
        </p>
      )}
    </div>
  )
}

/** A question's Tip (while `tips`: the attempt is open) and Ask actions with its thread inline. */
export function ExamThread({ q, draft, target, tips = true }: { q: ExamQuestion; draft: string; target: ChatTarget; tips?: boolean }) {
  const thread = q.thread ?? []   // a served exam from before chats has no thread
  const chat = useChat(target, q.id, thread)
  const [asking, setAsking] = useState(false)
  const [message, setMessage] = useDraft(useQuoteFor(target.attempt, q.id), () => setAsking(true))
  const send = async () => { if (await chat.run('question', message)) setMessage('') }

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        {tips && <TipButton used={q.tips ?? 0} busy={chat.busy} onTip={() => void chat.run('tip', draft)} />}
        <button type="button" onClick={() => setAsking((a) => !a)} className="rounded border px-2 py-1 text-sm">Ask</button>
      </div>
      {asking && <AskBox label="Message about this question" message={message} onChange={setMessage} busy={chat.busy} onSend={() => void send()} />}
      {chat.refusal && <p role="alert" className="text-failed-fg">{chat.refusal}</p>}
      <Turns thread={thread} live={chat.live} onRetry={(kind, text) => void chat.run(kind, text)} />
    </div>
  )
}

/** The general chat of the latest attempt, in a drawer on the right, a bottom sheet below `md`. Earlier attempts' general
 *  chats show above it, read only. */
export function GeneralChat({ target, thread, earlier }: { target: ChatTarget; thread: ExamTurn[]; earlier: { attempt: number; thread: ExamTurn[] }[] }) {
  const chat = useChat(target, null, thread)
  const [open, setOpen] = useState(false)
  const [message, setMessage] = useDraft(useQuoteFor(target.attempt, null), () => setOpen(true))
  const send = async () => { if (await chat.run('general', message)) setMessage('') }
  return (
    <>
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)}
        className="fixed bottom-4 right-4 z-10 rounded border border-border bg-surface px-3 py-2 shadow">
        Chat
      </button>
      {open && (
        <aside data-chat aria-label="General chat"
          className="fixed inset-x-0 bottom-0 z-20 max-h-[70vh] space-y-2 overflow-y-auto border-t border-border bg-surface p-4 shadow md:inset-y-0 md:left-auto md:max-h-none md:w-96 md:border-l md:border-t-0">
          <div className="flex items-center justify-between">
            <h2 className="font-medium">General chat</h2>
            <button type="button" onClick={() => setOpen(false)} className="rounded border px-2 py-1 text-sm">Close</button>
          </div>
          {earlier.filter((e) => e.thread.length).map((e) => (
            <section key={e.attempt} className="space-y-2">
              <h3 className="text-sm text-ink-muted">Attempt {e.attempt}</h3>
              {e.thread.map((t) => <Turn key={t.id} turn={t} />)}
            </section>
          ))}
          <Turns thread={thread} live={chat.live} onRetry={(kind, text) => void chat.run(kind, text)} />
          {chat.refusal && <p role="alert" className="text-failed-fg">{chat.refusal}</p>}
          <AskBox label="Message to the general chat" message={message} onChange={setMessage} busy={chat.busy} onSend={() => void send()} />
        </aside>
      )}
    </>
  )
}

function TipButton({ used, busy, onTip }: { used: number; busy: boolean; onTip: () => void }) {
  return (
    <button type="button" disabled={busy || used >= TIP_CAP} onClick={onTip} className="rounded border px-2 py-1 text-sm disabled:opacity-50">
      Tip {used} / {TIP_CAP}
    </button>
  )
}

function AskBox({ label, message, onChange, busy, onSend }: {
  label: string; message: string; onChange: (m: string) => void; busy: boolean; onSend: () => void
}) {
  return (
    <div className="flex gap-2">
      <textarea aria-label={label} rows={2} value={message}
        onChange={(e) => onChange(e.target.value)} className="w-full rounded border border-ink-muted bg-transparent p-2" />
      <button type="button" disabled={busy || !message.trim()} onClick={onSend} className="rounded border px-2 py-1 text-sm disabled:opacity-50">Send</button>
    </div>
  )
}

/** The stored thread, then the reply as it streams until the stored turn has it. */
function Turns({ thread, live, onRetry }: { thread: ExamTurn[]; live: Live | null; onRetry: (kind: ExamTurn['kind'], text: string) => void }) {
  const showLive = live && !thread.some((t) => t.id === live.id)
  return (
    <>
      {thread.map((t) => (
        <Turn key={t.id} turn={t.id === live?.id && t.status === 'running' ? { ...t, text: live.text, error: live.error } : t}
          onRetry={() => onRetry(t.kind, t.message)} />
      ))}
      {showLive && <Turn turn={live} onRetry={() => onRetry(live.kind, live.message)} />}
    </>
  )
}
