import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router'
import { PendingBadge } from '../components/PendingBadge'
import { SpecPanel } from '../components/SpecPanel'
import { Terminal } from '../components/Terminal'
import { queryKeys } from '../hooks/queryKeys'
import { usePendingIntents, useTaskDetail } from '../hooks/useResources'
import { usePersistedTerminalHeight } from '../hooks/usePersistedTerminalHeight'
import { api, ApiError } from '../lib/api'
import { relativeTime, stageLabel } from '../lib/format'
import { useUiStore } from '../store/ui'

export function TaskPage() {
  const raw = useParams().issue
  const issue = Number(raw)
  // /task/abc would otherwise request /api/task/NaN.
  if (!Number.isInteger(issue) || issue <= 0) {
    return (
      <p className="p-4 text-red-600">
        not found — “{raw}” is not a task number
      </p>
    )
  }
  return <TaskView issue={issue} />
}

function TaskView({ issue }: { issue: number }) {
  const detailQuery = useTaskDetail(issue)
  const intentsQuery = usePendingIntents()
  const queryClient = useQueryClient()
  const [replyText, setReplyText] = useState('')
  // Inline error channel, same convention as QueuePage/QueueTable: park/kill/
  // retry/reply return real 404s and 5xx, and a swallowed failure is
  // pixel-identical to success — no badge, no error, operator misled.
  const [actionError, setActionError] = useState<string | null>(null)
  // Kill terminates a live agent, so it is two-step. Not window.confirm: a
  // native modal blocks the page (and the polling console behind it).
  const [killArmed, setKillArmed] = useState(false)
  const terminalOpenFor = useUiStore((s) => s.terminalOpenFor)
  const setTerminalOpenFor = useUiStore((s) => s.setTerminalOpenFor)
  const { ref: paneWrapRef, height: terminalHeight } = usePersistedTerminalHeight()
  const terminalOpen = terminalOpenFor === issue

  const intent = useMutation({
    mutationFn: ({ run }: { run: () => Promise<unknown>; isReply?: boolean }) => run(),
    onSuccess: (_data, { isReply }) => {
      setActionError(null)
      // Only the reply action owns the textarea — park/kill/retry must not
      // wipe text the operator already typed.
      if (isReply) setReplyText('')
      // Only refetch pending intents; task state changes when the
      // dispatcher applies the intent — never optimistically.
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
    },
    onError: (err) =>
      setActionError(err instanceof ApiError ? err.detail : String(err)),
  })

  const runIntent = (run: () => Promise<unknown>, isReply = false) => {
    setKillArmed(false)
    intent.mutate({ run, isReply })
  }

  if (detailQuery.isPending) return <p className="p-4 text-gray-500">loading task…</p>
  if (detailQuery.isError) {
    return <p className="p-4 text-red-600">{detailQuery.error.message}</p>
  }

  const { card, pane_tail, session_alive, worktree } = detailQuery.data
  const myIntents = (intentsQuery.data?.intents ?? []).filter(
    (i) => i.issue === issue,
  )
  const busy = intent.isPending

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

      {card.stage === 'awaiting-spec-review' && (
        <SpecPanel
          issue={issue}
          busy={busy}
          onApprove={() =>
            runIntent(() => api.reply(issue, 'Approved — proceed.'))
          }
        />
      )}

      {/* Terminal.tsx's dead overlay only fires when a session dies WHILE
          attached; on the load path the page owns this state. */}
      {card.park !== '' ? (
        <div
          data-testid="parked-panel"
          className="rounded border border-purple-300 bg-purple-50 px-3 py-2 text-sm text-purple-800"
        >
          <p className="font-medium">
            parked ({card.park}) — reply below to wake this task
          </p>
          {card.park_note !== '' && (
            <p className="mt-1 whitespace-pre-wrap">{card.park_note}</p>
          )}
        </div>
      ) : (
        !session_alive && (
          <p
            data-testid="session-dead"
            className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm font-medium text-amber-800"
          >
            session task-{issue} is not running
          </p>
        )
      )}

      {terminalOpen && session_alive ? (
        <Terminal issue={issue} />
      ) : (
        <div
          ref={paneWrapRef}
          data-testid="terminal-pane-wrap"
          className="w-full resize-y overflow-auto"
          style={{ height: terminalHeight }}
        >
          <pre
            data-testid="pane-tail"
            className="h-full overflow-auto rounded bg-gray-900 p-3 font-mono text-xs text-gray-100"
          >
            {pane_tail}
          </pre>
        </div>
      )}

      {actionError && (
        <p data-testid="action-error" className="text-sm text-red-600">
          {actionError}
        </p>
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
          disabled={replyText.trim() === '' || busy}
          onClick={() => runIntent(() => api.reply(issue, replyText), true)}
        >
          {card.park !== '' ? 'Send reply & wake' : 'Send reply'}
        </button>
        <button
          type="button"
          className="rounded border px-3 py-1.5 text-sm disabled:opacity-50"
          disabled={!session_alive && !terminalOpen}
          onClick={() => setTerminalOpenFor(terminalOpen ? null : issue)}
        >
          {terminalOpen ? 'Detach terminal' : 'Attach terminal'}
        </button>
        {/* Sessions are launched with --remote-control task-<N>, so they are
            reachable from claude.ai/code and the Claude mobile app by name.
            This is the mobile interaction path — the embedded terminal is the
            desktop escape hatch. Not gated on session_alive: the conversation
            stays readable there after the tmux session dies. */}
        <a
          href="https://claude.ai/code"
          target="_blank"
          rel="noreferrer"
          className="rounded border px-3 py-1.5 text-sm text-blue-700"
        >
          Open in Claude ↗
        </a>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50"
          disabled={busy}
          onClick={() => runIntent(() => api.park(issue))}>Park now</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50"
          disabled={busy}
          onClick={() => runIntent(() => api.resume(issue))}>Resume now</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50"
          disabled={busy}
          onClick={() => runIntent(() => api.retry(issue))}>Retry</button>
        <button
          type="button"
          className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 disabled:opacity-50"
          disabled={busy}
          onClick={() =>
            killArmed ? runIntent(() => api.kill(issue)) : setKillArmed(true)
          }
        >
          {killArmed ? 'Confirm kill?' : 'Kill'}
        </button>
        {killArmed && (
          <button
            type="button"
            className="rounded border px-3 py-1.5 text-sm"
            onClick={() => setKillArmed(false)}
          >
            Cancel kill
          </button>
        )}
      </div>
    </div>
  )
}
