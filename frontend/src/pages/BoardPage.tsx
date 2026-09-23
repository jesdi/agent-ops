import { useSearchParams } from 'react-router'
import { BoardColumn, type DraggedCard } from '../components/BoardColumn'
import { BoardHeader } from '../components/BoardHeader'
import { ColumnTabs } from '../components/ColumnTabs'
import { CountStrip, type EmptyColumn } from '../components/CountStrip'
import { GhostCardView } from '../components/GhostCard'
import type { Column, GhostCard, NextClaimView, PendingIntent, Zone } from '../lib/api'
import { useBoardSnapshot, usePendingIntents, useTasks } from '../hooks/useResources'
import { useQueueActions } from '../hooks/useQueueActions'
import { useWontDo } from '../hooks/useWontDo'

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

/** Stale indicator and action error ride on the Queued header, or on the
 *  Queued chip when it is empty, so degraded state is never hidden. */
function QueuedHeaderExtra({ stale, error }: { stale: boolean | undefined; error: QueueActions['queueError'] }) {
  return (
    <>
      {stale && (
        <span
          data-testid="queue-stale"
          className="rounded bg-waiting-bg px-1.5 text-xs font-normal text-waiting-fg"
          title="queue order may be outdated"
        >
          stale
        </span>
      )}
      {error && (
        <span
          data-testid="queue-error"
          className="rounded bg-failed-bg px-1.5 text-xs font-normal text-failed-fg"
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

/** Zone and column order are the read model's; the page only groups
 *  consecutive columns that share a zone, so it holds no ordering of its own. */
function zonesInOrder(columns: Column[]): { zone: Zone; columns: Column[] }[] {
  const zones: { zone: Zone; columns: Column[] }[] = []
  for (const column of columns) {
    const last = zones.at(-1)
    if (last?.zone === column.zone) last.columns.push(column)
    else zones.push({ zone: column.zone, columns: [column] })
  }
  return zones
}

const ZONE_TITLE: Record<Zone, string> = { 'needs-you': 'Needs you', pipeline: 'Pipeline' }

/** Needs you is the one deliberate emphasis: a warm tint and a heavier header. */
const ZONE_STYLE: Record<Zone, { section: string; header: string }> = {
  'needs-you': {
    section: 'rounded-lg bg-surface-attention p-2',
    header: 'text-sm font-semibold text-ink',
  },
  pipeline: {
    section: 'p-2',
    header: 'text-sm font-medium text-ink-muted',
  },
}

function WontDoConfirm({ card, error, busy, onKeep, onConfirm }: {
  card: DraggedCard; error: string | null; busy: boolean
  onKeep: () => void; onConfirm: (card: DraggedCard) => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim">
      <div
        data-testid="wont-do-confirm"
        className="w-96 rounded border bg-surface-raised p-4 shadow-lg"
      >
        <p className="text-sm">
          Move <span className="font-medium">#{card.issue} {card.title}</span>{' '}
          to Wont do? The board card is retired and the issue closes as not planned.
        </p>
        {error && <p className="mt-2 text-xs text-failed-fg">{error}</p>}
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
            className="rounded border border-failed-fg/30 px-3 py-1.5 text-sm text-failed-fg disabled:opacity-50"
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
  const queue = useQueueActions()
  const wontDo = useWontDo()
  const [params, setParams] = useSearchParams()

  const board = boardQuery.data ?? snapshotQuery.data
  if (!board && (boardQuery.isPending || snapshotQuery.isPending)) return <p className="p-4 text-ink-muted">loading board…</p>
  if (!board) {
    return <p className="p-4 text-failed-fg">board unavailable: {boardQuery.error?.message ?? snapshotQuery.error?.message}</p>
  }

  const upcoming = boardQuery.data?.upcoming ?? []
  const pendingByKey = pendingIntentsByKey(intentsQuery.data?.intents ?? [])
  const queuedMarkers = <QueuedHeaderExtra stale={boardQuery.data?.upcoming_stale} error={queue.queueError} />
  const queuedExtras = {
    extra: <GhostStack upcoming={upcoming} nextClaim={boardQuery.data?.next_claim} queue={queue} />,
    extraCount: upcoming.length,
    headerExtra: queuedMarkers,
  }
  const dropFor = (key: string) => (key === 'wont-do' ? wontDo.propose : undefined)
  // A column with no cards and no ghosts is a chip; anything occupied stays
  // in the row, so a card can never disappear into the strip.
  const occupied = (column: Column) =>
    column.cards.length > 0 || (column.key === 'queued' && upcoming.length > 0)
  const inRow = board.columns.filter(occupied)
  const count = (column: Column) => column.cards.length + (column.key === 'queued' ? upcoming.length : 0)
  // Phones show one column: the one named in the URL, so back and reload
  // land on the same tab, else the first occupied one. A key that is not
  // (or no longer) in the row falls back the same way.
  const active = (inRow.find((c) => c.key === params.get('column')) ?? inRow[0])?.key
  // Replace, not push: switching tabs is not a navigation back should undo.
  const selectColumn = (key: string) =>
    setParams((p) => { p.set('column', key); return p }, { replace: true })
  const phoneHidden = (shown: boolean) => (shown ? '' : 'max-md:hidden')
  const empty: EmptyColumn[] = board.columns.filter((c) => !occupied(c)).map((column) => ({
    column,
    markers: column.key === 'queued' ? queuedMarkers : undefined,
    onCardDrop: dropFor(column.key),
  }))

  // From md up the board is exactly the viewport below the nav (AppShell is a
  // min-h-dvh flex column; a zero-basis grow item cannot push it taller), so
  // the page never scrolls: the row scrolls sideways with its scrollbar on the
  // bottom edge, and each column body scrolls on its own. Below md the page
  // scrolls, a tab row picks the column, and only that column is shown, at
  // full width.
  return (
    <div className="flex flex-col gap-4 p-4 md:min-h-0 md:grow md:basis-0 md:pb-0">
      <BoardHeader board={board} />
      <ColumnTabs
        tabs={inRow.map((c) => ({ key: c.key, title: c.title, count: count(c) }))}
        active={active}
        onSelect={selectColumn}
      />
      <CountStrip columns={empty} />
      <div data-testid="board-row" className="-mx-4 flex gap-4 overflow-x-auto px-4 pb-4 md:min-h-0 md:grow md:basis-0">
        {zonesInOrder(inRow).map(({ zone, columns }) => (
          <section
            key={zone}
            data-testid={`zone-${zone}`}
            aria-labelledby={`zone-${zone}-title`}
            // Hugs its tallest column, capped at the row height.
            className={`flex max-h-full min-h-0 shrink-0 flex-col gap-2 self-start max-md:w-full ${ZONE_STYLE[zone].section} ${
              phoneHidden(columns.some((c) => c.key === active))}`}
          >
            <h2 id={`zone-${zone}-title`} className={`px-1 ${ZONE_STYLE[zone].header}`}>
              {ZONE_TITLE[zone]}
            </h2>
            <div className="flex min-h-0 flex-auto gap-4">
              {columns.map((column) => (
                <BoardColumn
                  key={column.key}
                  column={column}
                  pendingByKey={pendingByKey}
                  {...(column.key === 'queued' ? queuedExtras : {})}
                  onCardDrop={dropFor(column.key)}
                  className={phoneHidden(column.key === active)}
                />
              ))}
            </div>
          </section>
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
