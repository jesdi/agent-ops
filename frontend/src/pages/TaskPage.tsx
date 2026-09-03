import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useParams } from 'react-router'
import { DescriptionPanel } from '../components/DescriptionPanel'
import { MessageThread } from '../components/MessageThread'
import { PendingBadge } from '../components/PendingBadge'
import { SpecPanel } from '../components/SpecPanel'
import { TerminalHistory } from '../components/TerminalHistory'
import { queryKeys } from '../hooks/queryKeys'
import { useQueueActions } from '../hooks/useQueueActions'
import { useIssueDescription, usePendingIntents, useTaskDetail } from '../hooks/useResources'
import { api, ApiError } from '../lib/api'
import { formatDuration, relativeTime, stageLabel } from '../lib/format'

export function TaskPage() {
  const { target, issue: rawIssue } = useParams()
  // An empty/missing target would otherwise request /api/task//<issue>.
  if (!target) {
    return (
      <p className="p-4 text-red-600">
        not found — a task needs a target
      </p>
    )
  }
  const issue = Number(rawIssue)
  // /task/x/abc would otherwise request /api/task/x/NaN.
  if (!Number.isInteger(issue) || issue <= 0) {
    return (
      <p className="p-4 text-red-600">
        not found — "{rawIssue}" is not a task number
      </p>
    )
  }
  return <TaskView target={target} issue={issue} />
}

function TaskView({ target, issue }: { target: string; issue: number }) {
  const detailQuery = useTaskDetail(target, issue)
  const intentsQuery = usePendingIntents()
  const queryClient = useQueryClient()
  const [replyText, setReplyText] = useState('')
  // Inline error channel, same convention as useQueueActions: park/kill/
  // retry/reply return real 404s and 5xx, and a swallowed failure is
  // pixel-identical to success — no badge, no error, operator misled.
  const [actionError, setActionError] = useState<string | null>(null)
  // Kill terminates a live agent, so it is two-step. Not window.confirm: a
  // native modal blocks the page (and the polling console behind it).
  const [killArmed, setKillArmed] = useState(false)
  // Won't-do retires the task for good (board → Wont do, issue closed as
  // not planned), so it takes the same two-step confirm as kill.
  const [wontDoArmed, setWontDoArmed] = useState(false)
  // Local, not in the store: navigating to another task must open on its
  // live tail, never on a history view left behind by the previous one.
  const [showHistory, setShowHistory] = useState(false)
  useEffect(() => setShowHistory(false), [target, issue])

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
    setWontDoArmed(false)
    intent.mutate({ run, isReply })
  }

  if (detailQuery.isPending) return <p className="p-4 text-gray-500">loading task…</p>
  if (detailQuery.isError) {
    if (detailQuery.error instanceof ApiError && detailQuery.error.status === 404) {
      return <GhostTaskView target={target} issue={issue} />
    }
    return <p className="p-4 text-red-600">{detailQuery.error.message}</p>
  }

  const { card, pane_tail, session_alive, worktree, messages, delivery_contract } =
    detailQuery.data
  // A target-less legacy intent (written before the target field existed)
  // cannot be attributed to one target over another, so it matches by issue
  // alone; anything else must match this exact target too.
  const myIntents = (intentsQuery.data?.intents ?? []).filter(
    (i) => i.issue === issue && (i.target === target || i.target === ''),
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

      {detailQuery.data.timeline.length > 0 && (
        <div data-testid="stage-timeline" className="flex flex-wrap gap-2 text-xs text-gray-500">
          {detailQuery.data.timeline.map((seg, i) => (
            <span key={i}
              className={`rounded px-1.5 py-0.5 ${seg.kind === 'parked' ? 'bg-purple-50 text-purple-700' : 'bg-gray-100'}`}>
              {seg.label} {formatDuration(seg.seconds)}{seg.ongoing ? ' — ongoing' : ''}
            </span>
          ))}
        </div>
      )}

      <p className="font-mono text-xs text-gray-500">{worktree}</p>

      <DescriptionPanel target={target} issue={issue} />

      {card.stage === 'awaiting-spec-review' && (
        <SpecPanel
          target={target}
          issue={issue}
          busy={busy}
          onApprove={() =>
            runIntent(() => api.reply(target, issue, 'Approved — proceed.'))
          }
        />
      )}

      {/* On the load path the page owns the dead/parked state; the console
          below is read-only either way. */}
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

      {/* Read-only console: the polled pane tail, or the scrollable history
          (snapshot-backed once the session is dead). Interactive attach is
          external — herdr from a terminal — so the board never holds a PTY
          and the dispatcher never waits on a viewer. */}
      <div data-testid="console" className="h-96 w-full">
        {showHistory ? (
          <TerminalHistory target={target} issue={issue} onClose={() => setShowHistory(false)} />
        ) : (
          <pre
            data-testid="pane-tail"
            className="h-full overflow-auto rounded bg-gray-900 p-3 font-mono text-xs text-gray-100"
          >
            {pane_tail}
          </pre>
        )}
      </div>
      <p data-testid="attach-guidance" className="text-xs text-gray-500">
        To interact with the session, attach from a terminal:{' '}
        <code>herdr --remote box</code> (desktop) or Moshi (phone). Attach to
        watch; reply here or on Telegram.
      </p>

      {actionError && (
        <p data-testid="action-error" className="text-sm text-red-600">
          {actionError}
        </p>
      )}

      <MessageThread messages={messages} />

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
          <span data-testid="delivery-contract" className="mt-1 text-xs text-gray-500">
            {delivery_contract}
          </span>
        </label>
        <button
          type="button"
          className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          disabled={replyText.trim() === '' || busy}
          onClick={() => runIntent(() => api.reply(target, issue, replyText), true)}
        >
          {card.park !== '' ? 'Send reply & wake' : 'Send message'}
        </button>
        <button
          type="button"
          className="rounded border px-3 py-1.5 text-sm"
          onClick={() => setShowHistory((v) => !v)}
        >
          {showHistory ? 'Live view' : 'Show history'}
        </button>
        {/* Sessions are launched with --remote-control task-<N>, so they are
            reachable from claude.ai/code and the Claude mobile app by name.
            This is the mobile interaction path — `herdr --remote box` is the
            desktop escape hatch. Not gated on session_alive: the conversation
            stays readable there after the session dies. */}
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
          onClick={() => runIntent(() => api.park(target, issue))}>Park now</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50"
          disabled={busy}
          onClick={() => runIntent(() => api.resume(target, issue))}>Resume now</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50"
          disabled={busy}
          onClick={() => runIntent(() => api.retry(target, issue))}>Retry</button>
        <button
          type="button"
          className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 disabled:opacity-50"
          disabled={busy}
          onClick={() =>
            killArmed ? runIntent(() => api.kill(target, issue)) : setKillArmed(true)
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
        <button
          type="button"
          className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 disabled:opacity-50"
          disabled={busy}
          onClick={() =>
            wontDoArmed ? runIntent(() => api.cancel(target, issue)) : setWontDoArmed(true)
          }
        >
          {wontDoArmed ? "Confirm won't do?" : "Won't do"}
        </button>
        {wontDoArmed && (
          <button
            type="button"
            className="rounded border px-3 py-1.5 text-sm"
            onClick={() => setWontDoArmed(false)}
          >
            Keep task
          </button>
        )}
      </div>
    </div>
  )
}

function GhostTaskView({ target, issue }: { target: string; issue: number }) {
  const desc = useIssueDescription(target, issue, true)
  const { queueError, busy, boost, next, ready } = useQueueActions()
  return (
    <div data-testid="ghost-task-view" className="flex flex-col gap-4 p-4">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">{desc.data?.title || `#${issue}`}</h1>
        <span className="rounded border border-dashed border-gray-300 px-2 py-0.5 text-sm text-gray-500">
          upcoming — not claimed yet
        </span>
      </header>
      <DescriptionPanel target={target} issue={issue} defaultOpen />
      {queueError && <p data-testid="queue-error" className="text-sm text-red-600">{queueError}</p>}
      <div className="flex gap-2">
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50" disabled={busy} onClick={() => boost(issue, 1)}>Boost</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50" disabled={busy} onClick={() => boost(issue, -1)}>Demote</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50" disabled={busy} onClick={() => next(issue)}>Next</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50" disabled={busy} onClick={() => ready(issue)}>Ready</button>
      </div>
    </div>
  )
}
