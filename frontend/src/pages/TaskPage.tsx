import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router'
import { PendingBadge } from '../components/PendingBadge'
import { Terminal } from '../components/Terminal'
import { queryKeys } from '../hooks/queryKeys'
import { usePendingIntents, useTaskDetail } from '../hooks/useResources'
import { api } from '../lib/api'
import { relativeTime, stageLabel } from '../lib/format'
import { useUiStore } from '../store/ui'

export function TaskPage() {
  const issue = Number(useParams().issue)
  const detailQuery = useTaskDetail(issue)
  const intentsQuery = usePendingIntents()
  const queryClient = useQueryClient()
  const [replyText, setReplyText] = useState('')
  const terminalOpen = useUiStore((s) => s.terminalOpen)
  const setTerminalOpen = useUiStore((s) => s.setTerminalOpen)

  const intent = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => {
      setReplyText('')
      // Only refetch pending intents; task state changes when the
      // dispatcher applies the intent — never optimistically.
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
    },
  })

  if (detailQuery.isPending) return <p className="p-4 text-gray-500">loading task…</p>
  if (detailQuery.isError) {
    return <p className="p-4 text-red-600">{detailQuery.error.message}</p>
  }

  const { card, pane_tail, session_alive, worktree } = detailQuery.data
  const myIntents = (intentsQuery.data?.intents ?? []).filter(
    (i) => i.issue === issue,
  )

  return (
    <div className="flex flex-col gap-4 p-4">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">{card.title}</h1>
        <span className="text-sm text-gray-500">
          {card.target}#{card.issue} · {stageLabel(card.stage)} · {card.model} ·
          branch {card.branch} · updated {relativeTime(card.updated_at)}
        </span>
        {card.park !== '' && (
          <span className="rounded bg-purple-100 px-2 py-0.5 text-sm text-purple-700">
            parked: {card.park}
          </span>
        )}
        {myIntents.map((i) => (
          <PendingBadge key={`${i.action}-${i.created_at}`} action={i.action} />
        ))}
      </header>

      <p className="font-mono text-xs text-gray-500">{worktree}</p>

      {terminalOpen && session_alive ? (
        <Terminal issue={issue} />
      ) : (
        <pre
          data-testid="pane-tail"
          className="max-h-80 overflow-auto rounded bg-gray-900 p-3 font-mono text-xs text-gray-100"
        >
          {pane_tail}
        </pre>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col text-sm">
          Reply
          <textarea
            aria-label="Reply"
            value={replyText}
            onChange={(e) => setReplyText(e.target.value)}
            className="mt-1 w-96 rounded border border-gray-300 p-2 font-mono text-xs"
            rows={3}
          />
        </label>
        <button
          type="button"
          className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          disabled={replyText.trim() === '' || intent.isPending}
          onClick={() => intent.mutate(() => api.reply(issue, replyText))}
        >
          Send reply
        </button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm"
          onClick={() => setTerminalOpen(!terminalOpen)}>
          {terminalOpen ? 'Detach terminal' : 'Attach terminal'}
        </button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm"
          onClick={() => intent.mutate(() => api.park(issue))}>Park now</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm"
          onClick={() => intent.mutate(() => api.resume(issue))}>Resume now</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm"
          onClick={() => intent.mutate(() => api.retry(issue))}>Retry</button>
        <button type="button" className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-700"
          onClick={() => intent.mutate(() => api.kill(issue))}>Kill</button>
      </div>
    </div>
  )
}
