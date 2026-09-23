import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import type { PendingIntent, TaskDetail } from '../lib/api'
import { useParams } from 'react-router'
import { ArtifactsPanel } from '../components/ArtifactsPanel'
import { AdmissionWarning } from '../components/AdmissionWarning'
import { DescriptionPanel } from '../components/DescriptionPanel'
import { MessageThread } from '../components/MessageThread'
import { PendingBadge } from '../components/PendingBadge'
import { RequestPanel } from '../components/RequestPanel'
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
      <p className="p-4 text-failed-fg">
        not found — a task needs a target
      </p>
    )
  }
  const issue = Number(rawIssue)
  // /task/x/abc would otherwise request /api/task/x/NaN.
  if (!Number.isInteger(issue) || issue <= 0) {
    return (
      <p className="p-4 text-failed-fg">
        not found — "{rawIssue}" is not a task number
      </p>
    )
  }
  return <TaskView target={target} issue={issue} />
}

function TaskView({ target, issue }: { target: string; issue: number }) {
  const detailQuery = useTaskDetail(target, issue)
  const intentsQuery = usePendingIntents()
  const actions = useTaskActions(target, issue)
  // Local, not in the store: navigating to another task must open on its
  // live tail, never on a history view left behind by the previous one.
  const [showHistory, setShowHistory] = useState(false)
  useEffect(() => setShowHistory(false), [target, issue])

  return <TaskQueryView target={target} issue={issue} detailQuery={detailQuery}
    intentsQuery={intentsQuery} actions={actions} showHistory={showHistory}
    setShowHistory={setShowHistory} />
}

function TaskQueryView({ target, issue, detailQuery, intentsQuery, actions,
  showHistory, setShowHistory }: {
  target: string
  issue: number
  detailQuery: ReturnType<typeof useTaskDetail>
  intentsQuery: ReturnType<typeof usePendingIntents>
  actions: ReturnType<typeof useTaskActions>
  showHistory: boolean
  setShowHistory: Dispatch<SetStateAction<boolean>>
}) {
  if (detailQuery.isPending) return <p className="p-4 text-ink-muted">loading task…</p>
  if (detailQuery.isError) {
    if (detailQuery.error instanceof ApiError && detailQuery.error.status === 404) {
      return <GhostTaskView target={target} issue={issue} />
    }
    return <p className="p-4 text-failed-fg">{detailQuery.error.message}</p>
  }
  return <LoadedTaskView target={target} issue={issue} detail={detailQuery.data}
    intents={intentsQuery.data?.intents ?? []} actions={actions}
    showHistory={showHistory} setShowHistory={setShowHistory} />
}

function LoadedTaskView({ target, issue, detail, intents, actions,
  showHistory, setShowHistory }: {
  target: string
  issue: number
  detail: TaskDetail
  intents: NonNullable<ReturnType<typeof usePendingIntents>['data']>['intents']
  actions: ReturnType<typeof useTaskActions>
  showHistory: boolean
  setShowHistory: Dispatch<SetStateAction<boolean>>
}) {
  const { busy, actionError, runIntent } = actions
  const { card, pane_tail, session_alive, worktree, messages, delivery_contract } = detail

  return (
    <div className="flex flex-col gap-4 p-4">
      <TaskHeader card={card} intents={intents} target={target} issue={issue} />
      {detail.track_when && (
        <p data-testid="track-when" className="text-xs text-ink-muted">{detail.track_when}</p>
      )}
      <StageTimeline timeline={detail.timeline} />

      <p className="break-all font-mono text-xs text-ink-muted">{worktree}</p>

      <DescriptionPanel target={target} issue={issue} />

      <RequestPanel
        target={target}
        issue={issue}
        busy={busy}
        onApprove={() =>
          runIntent(() => api.reply(target, issue, 'Approved — proceed.'))
        }
      />

      <ArtifactsPanel target={target} issue={issue} />

      {/* On the load path the page owns the dead/parked state; the console
          below is read-only either way. */}
      <SessionStatus card={card} sessionAlive={session_alive} issue={issue} />

      {/* Read-only console: the polled pane tail, or the scrollable history
          (snapshot-backed once the session is dead). Interactive attach is
          external — herdr from a terminal — so the board never holds a PTY
          and the dispatcher never waits on a viewer. */}
      <TaskConsole target={target} issue={issue} paneTail={pane_tail}
        showHistory={showHistory} setShowHistory={setShowHistory} />
      <p data-testid="attach-guidance" className="text-xs text-ink-muted">
        To interact with the session, attach from a terminal:{' '}
        <code>herdr --remote box</code> (desktop) or Moshi (phone). Attach to
        watch; reply here or on Telegram.
      </p>

      {actionError && (
        <p data-testid="action-error" className="text-sm text-failed-fg">
          {actionError}
        </p>
      )}

      {card.admission && (
        <AdmissionWarning target={target} issue={issue} admission={card.admission} />
      )}

      <MessageThread messages={messages} />

      <TaskControls target={target} issue={issue} card={card} deliveryContract={delivery_contract}
        actions={actions} showHistory={showHistory} setShowHistory={setShowHistory} />
    </div>
  )
}

function TaskHeader({ card, intents, target, issue }: {
  card: TaskDetail['card']
  intents: PendingIntent[]
  target: string
  issue: number
}) {
  // Target-less legacy intents match by issue alone; current intents must
  // match the exact target because issue numbers are repository-local.
  const myIntents = intents.filter(
    (intent) => intent.issue === issue && (intent.target === target || intent.target === ''),
  )
  return <header className="flex flex-wrap items-center gap-3">
    <h1 className="text-lg font-semibold">{card.title}</h1>
    <span className="text-sm text-ink-muted">
      {card.target}#{card.issue} · {stageLabel(card.stage)} · {card.model}
      {card.track && <> · track {card.track}</>} ·
      branch {card.branch} · updated {relativeTime(card.updated_at)}
    </span>
    {card.park !== '' && (
      <span className="rounded bg-parked-bg px-2 py-0.5 text-sm text-parked-fg">
        parked: {card.park}
      </span>
    )}
    {myIntents.map((intent) => (
      <PendingBadge key={`${intent.action}-${intent.created_at}`} action={intent.action} />
    ))}
  </header>
}

function StageTimeline({ timeline }: { timeline: TaskDetail['timeline'] }) {
  if (timeline.length === 0) return null
  return <div data-testid="stage-timeline" className="flex flex-wrap gap-2 text-xs text-ink-muted">
    {timeline.map((segment, index) => (
      <span key={index}
        className={`rounded px-1.5 py-0.5 ${segment.kind === 'parked' ? 'bg-parked-bg text-parked-fg' : 'bg-ink/10'}`}>
        {segment.label} {formatDuration(segment.seconds)}{segment.ongoing ? ' — ongoing' : ''}
      </span>
    ))}
  </div>
}

function TaskConsole({ target, issue, paneTail, showHistory, setShowHistory }: {
  target: string
  issue: number
  paneTail: string
  showHistory: boolean
  setShowHistory: Dispatch<SetStateAction<boolean>>
}) {
  return <div data-testid="console" className="h-96 w-full">
    {showHistory ? (
      <TerminalHistory target={target} issue={issue} onClose={() => setShowHistory(false)} />
    ) : (
      <pre data-testid="pane-tail"
        className="h-full overflow-auto rounded bg-ink p-3 font-mono text-xs text-surface dark:bg-surface-raised dark:text-ink">
        {paneTail}
      </pre>
    )}
  </div>
}

function GhostTaskView({ target, issue }: { target: string; issue: number }) {
  const desc = useIssueDescription(target, issue, true)
  const { queueError, busy, boost, next, ready } = useQueueActions()
  return (
    <div data-testid="ghost-task-view" className="flex flex-col gap-4 p-4">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">{desc.data?.title || `#${issue}`}</h1>
        <span className="rounded border border-dashed border-border px-2 py-0.5 text-sm text-ink-muted">
          upcoming — not claimed yet
        </span>
      </header>
      <DescriptionPanel target={target} issue={issue} defaultOpen />
      {queueError && <p data-testid="queue-error" className="text-sm text-failed-fg">{queueError}</p>}
      <div className="flex gap-2">
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50" disabled={busy} onClick={() => boost(issue, 1)}>Boost</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50" disabled={busy} onClick={() => boost(issue, -1)}>Demote</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50" disabled={busy} onClick={() => next(issue)}>Next</button>
        <button type="button" className="rounded border px-3 py-1.5 text-sm disabled:opacity-50" disabled={busy} onClick={() => ready(issue)}>Ready</button>
      </div>
    </div>
  )
}

/** Intent submission owns draft/error state and resets destructive confirmations. */
function useTaskActions(target: string, issue: number) {
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

  const intent = useMutation({
    mutationFn: ({ run }: { run: () => Promise<unknown>; isReply?: boolean }) => run(),
    onSuccess: (_data, { isReply }) => {
      setActionError(null)
      // Only the reply action owns the textarea — park/kill/retry must not
      // wipe text the operator already typed.
      if (isReply) setReplyText('')
      // Refetch pending intents; task state changes when the dispatcher
      // applies the intent — never optimistically.
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
      // A reply/resume can clear or replace the operator_request, so
      // invalidate it so stale approval/answers content disappears.
      void queryClient.invalidateQueries({ queryKey: queryKeys.request(target, issue) })
    },
    onError: (err) =>
      setActionError(err instanceof ApiError ? err.detail : String(err)),
  })

  const runIntent = (run: () => Promise<unknown>, isReply = false) => {
    setKillArmed(false)
    setWontDoArmed(false)
    intent.mutate({ run, isReply })
  }

  return { replyText, setReplyText, actionError, killArmed, setKillArmed,
    wontDoArmed, setWontDoArmed, busy: intent.isPending, runIntent }
}

function TaskControls({ target, issue, card, deliveryContract, actions, showHistory, setShowHistory }: {
  target: string
  issue: number
  card: TaskDetail['card']
  deliveryContract: string
  actions: ReturnType<typeof useTaskActions>
  showHistory: boolean
  setShowHistory: Dispatch<SetStateAction<boolean>>
}) {
  const { replyText, setReplyText, busy, runIntent, killArmed, setKillArmed, wontDoArmed, setWontDoArmed } = actions
  return (
    <div className="flex flex-wrap items-end gap-2">
      <label className="flex w-full max-w-lg flex-col text-sm">
        Reply
        <textarea
          aria-label="Reply"
          value={replyText}
          onChange={(e) => setReplyText(e.target.value)}
          className="mt-1 w-full rounded border border-border p-2 font-mono text-xs"
          rows={3}
        />
        <span data-testid="delivery-contract" className="mt-1 text-xs text-ink-muted">
          {deliveryContract}
        </span>
      </label>
      <button
        type="button"
        className="rounded bg-ink px-3 py-1.5 text-sm text-surface-raised disabled:opacity-50"
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
        className="rounded border px-3 py-1.5 text-sm"
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
      <ConfirmAction armed={killArmed} setArmed={setKillArmed} busy={busy}
        label="Kill" confirmation="Confirm kill?" cancel="Cancel kill"
        onConfirm={() => runIntent(() => api.kill(target, issue))} />
      <ConfirmAction armed={wontDoArmed} setArmed={setWontDoArmed} busy={busy}
        label="Won't do" confirmation="Confirm won't do?" cancel="Keep task"
        onConfirm={() => runIntent(() => api.cancel(target, issue))} />
    </div>
  )
}

function ConfirmAction({ armed, setArmed, busy, label, confirmation, cancel, onConfirm }: {
  armed: boolean
  setArmed: Dispatch<SetStateAction<boolean>>
  busy: boolean
  label: string
  confirmation: string
  cancel: string
  onConfirm: () => void
}) {
  return <>
    <button type="button"
      className="rounded border border-failed-fg/30 px-3 py-1.5 text-sm text-failed-fg disabled:opacity-50"
      disabled={busy} onClick={() => armed ? onConfirm() : setArmed(true)}>
      {armed ? confirmation : label}
    </button>
    {armed && <button type="button" className="rounded border px-3 py-1.5 text-sm"
      onClick={() => setArmed(false)}>{cancel}</button>}
  </>
}

function SessionStatus({ card, sessionAlive, issue }: {
  card: TaskDetail['card']
  sessionAlive: boolean
  issue: number
}) {
  return <>
    {card.park !== '' ? (
      <div
        data-testid="parked-panel"
        className="rounded border border-parked-fg/30 bg-parked-bg px-3 py-2 text-sm text-parked-fg"
      >
        <p className="font-medium">
          parked ({card.park}) — reply below to wake this task
        </p>
        {card.park_note !== '' && (
          <p className="mt-1 whitespace-pre-wrap">{card.park_note}</p>
        )}
      </div>
    ) : (
      !sessionAlive && (
        <p
          data-testid="session-dead"
          className="rounded border border-waiting-fg/30 bg-waiting-bg px-3 py-2 text-sm font-medium text-waiting-fg"
        >
          session task-{issue} is not running
        </p>
      )
    )}
  </>
}
