import { BoardColumn, type DraggedCard } from '../components/BoardColumn'
import { BoardHeader } from '../components/BoardHeader'
import { GhostCardView } from '../components/GhostCard'
import type { GhostCard, NextClaimView, PendingIntent } from '../lib/api'
import { useBoardSnapshot, usePendingIntents, useTasks } from '../hooks/useResources'
import { useQueueActions } from '../hooks/useQueueActions'
import { useWontDo } from '../hooks/useWontDo'
import { useUiStore } from '../store/ui'

type QueueActions = ReturnType<typeof useQueueActions>

/** An issue can carry several pending intents at once (park then kill).
 *  Collapsing to one would silently drop the rest — and TaskPage renders all
 *  of them, so the board must too. Keyed `${target}#${issue}` — issue numbers
 *  are per-target, so alpha#73 and beta#73 must not share a bucket. A legacy
 *  (target-less) intent lands under `#${issue}` (empty target) and
 *  BoardColumn matches it against any card with that issue number. */
function pendingIntentsByKey(intents: PendingIntent[]): Map<string, string[]> {
  const byKey = new Map<string, string[]>()
  for (const i of intents) {
    const key = `${i.target}#${i.issue}`
    byKey.set(key, [...(byKey.get(key) ?? []), i.action])
  }
  return byKey
}

/** Stale indicator and action error live in the column header so they are
 *  visible even when Queued is collapsed (headerExtra survives collapse). */
function QueuedHeaderExtra({ stale, error }: { stale: boolean | undefined; error: QueueActions['queueError'] }) {
  return (
    <>
      {stale && (
        <span
          data-testid="queue-stale"
          className="rounded bg-amber-100 px-1.5 text-xs font-normal text-amber-800"
          title="queue order may be outdated"
        >
          stale
        </span>
      )}
      {error && (
        <span
          data-testid="queue-error"
          className="rounded bg-red-100 px-1.5 text-xs font-normal text-red-700"
          title={error}
        >
          !</span>
      )}
    </>
  )
}

function GhostStack({ upcoming, nextClaim, queue }: {
  upcoming: GhostCard[]; nextClaim: NextClaimView | undefined; queue: QueueActions
}) {
  return (
    <>
      {queue.queueError && <p className="text-xs text-red-600">{queue.queueError}</p>}
      {/* busy disables every ghost's buttons at once: each action re-ranks the
          shared queue, so a second click would act on pre-mutation ranks and
          creates a last-writer-wins race on the error state. */}
      {upcoming.map((g) => (
        <GhostCardView
          /* Issue numbers are per-repo: alpha#73 and beta#73 can both be
             ghosts, so the key (and the next-badge match) needs the target. */
          key={`${g.target}#${g.number}`}
          ghost={g}
          busy={queue.busy}
          isNext={nextClaim?.verdict === 'will-claim' && nextClaim.next_issue === g.number && nextClaim.next_target === g.target}
          onBoost={(n, amount) => queue.boost(n, amount)}
          onNext={(n) => queue.next(n)}
          onReady={(n) => queue.ready(n)}
        />
      ))}
    </>
  )
}

function WontDoConfirm({ card, error, busy, onKeep, onConfirm }: {
  card: DraggedCard; error: string | null; busy: boolean
  onKeep: () => void; onConfirm: (card: DraggedCard) => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div
        data-testid="wont-do-confirm"
        className="w-96 rounded border bg-white p-4 shadow-lg"
      >
        <p className="text-sm">
          Move <span className="font-medium">#{card.issue} {card.title}</span>{' '}
          to Wont do? The board card is retired and the issue closes as not planned.
        </p>
        {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
        <div className="mt-3 flex justify-end gap-2">
          <button
            type="button"
            className="rounded border px-3 py-1.5 text-sm"
            onClick={onKeep}
          >
            Keep task
          </button>
          <button
            type="button"
            className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 disabled:opacity-50"
            disabled={busy}
            onClick={() => onConfirm(card)}
          >
            Confirm won't do?
          </button>
        </div>
      </div>
    </div>
  )
}

export function BoardPage() {
  const boardQuery = useTasks()
  const snapshotQuery = useBoardSnapshot(!boardQuery.data)
  const intentsQuery = usePendingIntents()
  const collapsedColumns = useUiStore((s) => s.collapsedColumns)
  const toggleColumn = useUiStore((s) => s.toggleColumn)
  const queue = useQueueActions()
  const wontDo = useWontDo()

  const board = boardQuery.data ?? snapshotQuery.data
  if (!board && (boardQuery.isPending || snapshotQuery.isPending)) return <p className="p-4 text-gray-500">loading board…</p>
  if (!board) {
    return <p className="p-4 text-red-600">board unavailable: {boardQuery.error?.message ?? snapshotQuery.error?.message}</p>
  }

  const upcoming = boardQuery.data?.upcoming ?? []
  const pendingByKey = pendingIntentsByKey(intentsQuery.data?.intents ?? [])
  const queuedExtras = {
    extra: <GhostStack upcoming={upcoming} nextClaim={boardQuery.data?.next_claim} queue={queue} />,
    extraCount: upcoming.length,
    headerExtra: <QueuedHeaderExtra stale={boardQuery.data?.upcoming_stale} error={queue.queueError} />,
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <BoardHeader board={board} />
      <div className="flex gap-4 overflow-x-auto pb-4">
        {board.columns.map((column) => (
          <BoardColumn
            key={column.key}
            column={column}
            pendingByKey={pendingByKey}
            collapsed={collapsedColumns[column.key] ?? false}
            onToggle={() => toggleColumn(column.key)}
            {...(column.key === 'queued' ? queuedExtras : {})}
            onCardDrop={column.key === 'wont-do' ? wontDo.propose : undefined}
          />
        ))}
      </div>
      {wontDo.candidate && (
        <WontDoConfirm
          card={wontDo.candidate}
          error={wontDo.error}
          busy={wontDo.busy}
          onKeep={wontDo.dismiss}
          onConfirm={wontDo.confirm}
        />
      )}
    </div>
  )
}
