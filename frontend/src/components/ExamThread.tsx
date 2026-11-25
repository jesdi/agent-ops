import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type ExamQuestion, type ExamTurn, type TurnBody } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'

/** Which exam attempt a question's chat belongs to. */
export type ChatTarget = { owner: string; name: string; pr: string; attempt: number; head_sha: string }

const TIP_CAP = 3

type Live = { id: string; kind: ExamTurn['kind']; message: string; text: string; error: string | null }

function Turn({ turn, onRetry }: { turn: Pick<ExamTurn, 'kind' | 'message' | 'text'> & { error: string | null }; onRetry: () => void }) {
  return (
    <div className="space-y-1 rounded border border-border p-2 text-sm">
      {turn.kind === 'question' && <p className="text-ink-muted">{turn.message}</p>}
      {turn.text && <p className="whitespace-pre-wrap">{turn.text}</p>}
      {turn.error && (
        <p role="alert" className="text-failed-fg">
          {turn.error} <button type="button" onClick={onRetry} className="rounded border px-2 py-0.5">Retry</button>
        </p>
      )}
    </div>
  )
}

/** A question's Tip (while `tips`: the attempt is open) and Ask actions with its thread inline. A turn runs on the console, so this only
 *  watches the reply stream; the stored thread, read again once the turn ends, is what stays. */
export function ExamThread({ q, draft, target, tips = true }: { q: ExamQuestion; draft: string; target: ChatTarget; tips?: boolean }) {
  const queryClient = useQueryClient()
  const thread = q.thread ?? []   // a served exam from before chats has no thread
  const [live, setLive] = useState<Live | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const [asking, setAsking] = useState(false)
  const busy = isBusy(live, thread)
  const { owner, name, pr } = target

  const run = async (kind: TurnBody['kind'], text: string) => {
    setRefusal(null)
    let id: string
    try {
      id = (await api.startTurn(owner, name, pr, { attempt: target.attempt, head_sha: target.head_sha, kind, question: q.id, message: text })).id
    } catch (e) {
      setRefusal((e as Error).message)
      return
    }
    setLive({ id, kind, message: text, text: '', error: null })
    const end = await api.followTurn(owner, name, pr, id, (delta) => setLive((l) => l && { ...l, text: l.text + delta }))
      .catch((e: Error): { status: 'failed'; error: string } => ({ status: 'failed', error: e.message }))
    if (end.status === 'failed') setLive((l) => l && { ...l, error: end.error })
    await queryClient.invalidateQueries({ queryKey: queryKeys.exam(owner, name, pr) })
    if (end.status === 'done') setLive(null)
  }

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        {tips && <TipButton used={q.tips ?? 0} busy={busy} onTip={() => void run('tip', draft)} />}
        <button type="button" onClick={() => setAsking((a) => !a)} className="rounded border px-2 py-1 text-sm">Ask</button>
      </div>
      {asking && <AskBox id={`ask-${q.id}`} busy={busy} onSend={(text) => void run('question', text)} />}
      {refusal && <p role="alert" className="text-failed-fg">{refusal}</p>}
      <Turns thread={thread} live={live} onRetry={(kind, text) => void run(kind, text)} />
    </div>
  )
}

function TipButton({ used, busy, onTip }: { used: number; busy: boolean; onTip: () => void }) {
  return (
    <button type="button" disabled={busy || used >= TIP_CAP} onClick={onTip} className="rounded border px-2 py-1 text-sm disabled:opacity-50">
      Tip {used} / {TIP_CAP}
    </button>
  )
}

/** A turn is running here: one this page streams, or one the stored thread still shows running. */
function isBusy(live: Live | null, thread: ExamTurn[]) {
  return live !== null && live.error === null || thread.some((t) => t.status === 'running')
}

function AskBox({ id, busy, onSend }: { id: string; busy: boolean; onSend: (text: string) => void }) {
  const [message, setMessage] = useState('')
  const send = () => {
    onSend(message)
    setMessage('')
  }
  return (
    <div className="flex gap-2">
      <textarea id={id} aria-label="Message about this question" rows={2} value={message}
        onChange={(e) => setMessage(e.target.value)} className="w-full rounded border border-ink-muted bg-transparent p-2" />
      <button type="button" disabled={busy || !message.trim()} onClick={send} className="rounded border px-2 py-1 text-sm disabled:opacity-50">Send</button>
    </div>
  )
}

/** The stored thread, then the reply as it streams until the stored turn has it. */
function Turns({ thread, live, onRetry }: { thread: ExamTurn[]; live: Live | null; onRetry: (kind: ExamTurn['kind'], text: string) => void }) {
  const showLive = live && !thread.some((t) => t.id === live.id)
  return (
    <>
      {thread.map((t) => (
        <Turn key={t.id} turn={t.id === live?.id && t.status === 'running' ? { ...t, text: live.text } : t}
          onRetry={() => onRetry(t.kind, t.message)} />
      ))}
      {showLive && <Turn turn={live} onRetry={() => onRetry(live.kind, live.message)} />}
    </>
  )
}
