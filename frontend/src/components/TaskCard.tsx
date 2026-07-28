import { Link } from 'react-router'
import type { TaskCard } from '../lib/api'
import { relativeTime, stageLabel } from '../lib/format'
import { PendingBadge } from './PendingBadge'

export function TaskCardView({ card, pendingActions }: {
  card: TaskCard
  /** Every pending intent on this issue — one badge each, never collapsed. */
  pendingActions?: readonly string[]
}) {
  return (
    <Link
      to={`/task/${card.issue}`}
      data-testid={`card-${card.issue}`}
      className="block rounded border border-gray-200 bg-white p-3 shadow-sm hover:border-gray-400"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-gray-500">
          {card.target}#{card.issue}
        </span>
        {(pendingActions ?? []).map((action, i) => (
          <PendingBadge key={`${action}-${i}`} action={action} />
        ))}
      </div>
      <p className="mt-1 text-sm font-medium">{card.title}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-500">
        <span>{stageLabel(card.stage)}</span>
        <span>{card.model}</span>
        {card.slot >= 0 && <span>slot {card.slot}</span>}
        {card.park !== '' && (
          <span className="rounded bg-purple-100 px-1.5 text-purple-700">
            parked: {card.park}
          </span>
        )}
        {card.park_note_pending && (
          <span className="rounded bg-blue-100 px-1.5 text-blue-700">
            notify pending
          </span>
        )}
        {card.attached && (
          <span className="rounded bg-emerald-100 px-1.5 text-emerald-700">
            attached
          </span>
        )}
        <span>{relativeTime(card.updated_at)}</span>
      </div>
    </Link>
  )
}
