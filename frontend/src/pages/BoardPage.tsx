import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { BoardColumn, type DraggedCard } from '../components/BoardColumn'
import { BudgetBar } from '../components/BudgetBar'
import { CapacityMeter } from '../components/CapacityMeter'
import { GhostCardView } from '../components/GhostCard'
import { NextClaimLine } from '../components/NextClaimLine'
import { formatDuration } from '../lib/format'
import { api, ApiError } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'
import { useBudget, usePendingIntents, useTasks } from '../hooks/useResources'
import { useQueueActions } from '../hooks/useQueueActions'
import { useUiStore } from '../store/ui'

export function BoardPage() {
  const boardQuery = useTasks()
  const budgetQuery = useBudget()
  const intentsQuery = usePendingIntents()
  const collapsedColumns = useUiStore((s) => s.collapsedColumns)
  const toggleColumn = useUiStore((s) => s.toggleColumn)
  const { queueError, busy, boost, next, ready } = useQueueActions()
  const queryClient = useQueryClient()
  // A drop on Wont do never fires an intent by itself: won't-do retires the
  // task for good, so the operator always confirms first (mirrors the
  // two-step kill on the task view — no window.confirm, it blocks polling).
  const [wontDoCandidate, setWontDoCandidate] = useState<DraggedCard | null>(null)
  const [wontDoError, setWontDoError] = useState<string | null>(null)
  const cancelMutation = useMutation({
    mutationFn: (card: DraggedCard) => api.cancel(card.issue, card.target),
    onSuccess: () => {
      setWontDoCandidate(null)
      setWontDoError(null)
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
    },
    onError: (err) =>
      setWontDoError(err instanceof ApiError ? err.detail : String(err)),
  })

  if (boardQuery.isPending) return <p className="p-4 text-gray-500">loading board…</p>
  if (boardQuery.isError) {
    return <p className="p-4 text-red-600">board unavailable: {boardQuery.error.message}</p>
  }

  const { columns, capacity, upcoming, upcoming_stale, next_claim } = boardQuery.data
  // An issue can carry several pending intents at once (park then kill).
  // Collapsing to one would silently drop the rest — and TaskPage renders
  // all of them, so the board must too.
  const pendingByIssue = new Map<number, string[]>()
  for (const i of intentsQuery.data?.intents ?? []) {
    pendingByIssue.set(i.issue, [...(pendingByIssue.get(i.issue) ?? []), i.action])
  }
  // Stale indicator and action error live in the column header so they are
  // visible even when Queued is collapsed (headerExtra survives collapse).
  const queuedHeaderExtra = (
    <>
      {upcoming_stale && (
        <span
          data-testid="queue-stale"
          className="rounded bg-amber-100 px-1.5 text-xs font-normal text-amber-800"
          title="queue order may be outdated"
        >
          stale
        </span>
      )}
      {queueError && (
        <span
          data-testid="queue-error"
          className="rounded bg-red-100 px-1.5 text-xs font-normal text-red-700"
          title={queueError}
        >
          !</span>
      )}
    </>
  )

  const ghostStack = (
    <>
      {queueError && <p className="text-xs text-red-600">{queueError}</p>}
      {/* busy disables every ghost's buttons at once: each action re-ranks the
          shared queue, so a second click would act on pre-mutation ranks and
          creates a last-writer-wins race on the error state. */}
      {upcoming.map((g) => (
        <GhostCardView
          /* Issue numbers are per-repo: alpha#73 and beta#73 can both be
             ghosts, so the key (and the next-badge match) needs the target. */
          key={`${g.target}#${g.number}`}
          ghost={g}
          busy={busy}
          isNext={next_claim.verdict === 'will-claim' && next_claim.next_issue === g.number && next_claim.next_target === g.target}
          onBoost={(n, amount) => boost(n, amount)}
          onNext={(n) => next(n)}
          onReady={(n) => ready(n)}
        />
      ))}
    </>
  )

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <CapacityMeter capacity={capacity} />
        {/* A failed /api/budget must not silently vanish the gauge — the
            operator would read "no gauge" as "nothing to worry about". */}
        {budgetQuery.isError ? (
          <span
            data-testid="budget-error"
            className="rounded border border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-800"
          >
            usage unknown — budget unavailable: {budgetQuery.error.message}
          </span>
        ) : (
          budgetQuery.data && <BudgetBar budget={budgetQuery.data} />
        )}
        <NextClaimLine nextClaim={boardQuery.data.next_claim} />
        {boardQuery.data.median_cycle_seconds != null && (
          <span className="text-sm text-gray-500">
            ≈{formatDuration(boardQuery.data.median_cycle_seconds)} per task
          </span>
        )}
      </div>
      <div className="flex gap-4 overflow-x-auto pb-4">
        {columns.map((column) => (
          <BoardColumn
            key={column.key}
            column={column}
            pendingByIssue={pendingByIssue}
            collapsed={collapsedColumns[column.key] ?? false}
            onToggle={() => toggleColumn(column.key)}
            extra={column.key === 'queued' ? ghostStack : undefined}
            extraCount={column.key === 'queued' ? upcoming.length : undefined}
            headerExtra={column.key === 'queued' ? queuedHeaderExtra : undefined}
            onCardDrop={column.key === 'wont-do' ? setWontDoCandidate : undefined}
          />
        ))}
      </div>
      {wontDoCandidate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
          <div
            data-testid="wont-do-confirm"
            className="w-96 rounded border bg-white p-4 shadow-lg"
          >
            <p className="text-sm">
              Move <span className="font-medium">#{wontDoCandidate.issue} {wontDoCandidate.title}</span>{' '}
              to Wont do? The board card is retired and the issue closes as not planned.
            </p>
            {wontDoError && <p className="mt-2 text-xs text-red-600">{wontDoError}</p>}
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                className="rounded border px-3 py-1.5 text-sm"
                onClick={() => { setWontDoCandidate(null); setWontDoError(null) }}
              >
                Keep task
              </button>
              <button
                type="button"
                className="rounded border border-red-300 px-3 py-1.5 text-sm text-red-700 disabled:opacity-50"
                disabled={cancelMutation.isPending}
                onClick={() => cancelMutation.mutate(wontDoCandidate)}
              >
                Confirm won't do?
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
