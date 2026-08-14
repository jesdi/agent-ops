import { Link } from 'react-router'
import type { TaskCard } from '../lib/api'
import { ACCENT_BORDER, type Accent } from '../lib/capacity'
import { formatDuration, relativeTime, stageLabel } from '../lib/format'
import { PendingBadge } from './PendingBadge'

export function TaskCardView({ card, pendingActions, accent = 'blue' }: {
  card: TaskCard
  /** Every pending intent on this issue — one badge each, never collapsed. */
  pendingActions?: readonly string[]
  /** Board-wide: amber once capacity is full. Computed once in BoardPage so
   *  every accented card agrees with the header meter. */
  accent?: Accent
}) {
  return (
    <Link
      to={`/task/${card.issue}`}
      data-testid={`card-${card.issue}`}
      className={`block rounded border border-gray-200 bg-white p-3 shadow-sm hover:border-gray-400 border-l-4 ${
        card.consuming_capacity ? ACCENT_BORDER[accent] : 'border-l-transparent hover:border-l-transparent'
      }`}
    >
      {/* Colour is never the only signal — and this must not be an aria-label
          on the Link, which would clobber its accessible name. */}
      {card.consuming_capacity && <span className="sr-only">holding a capacity unit</span>}
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
        {card.score != null && (
          <span className="rounded bg-gray-100 px-1.5 font-medium text-gray-600">
            score {card.score}
          </span>
        )}
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
        {card.feedback_pending && (
          <span className="rounded bg-amber-100 px-1.5 text-amber-700">
            feedback queued
          </span>
        )}
        {card.undelivered_messages > 0 && (
          <span
            data-testid="mail-badge"
            className="rounded bg-blue-100 px-1.5 text-blue-700"
            title="queued operator messages"
          >
            ✉ {card.undelivered_messages}
          </span>
        )}
        {card.wake_blocked && (
          <span className="rounded bg-amber-100 px-1.5 text-amber-800">
            waiting for a free slot
          </span>
        )}
        {card.attached && (
          <span className="rounded bg-emerald-100 px-1.5 text-emerald-700">
            attached
          </span>
        )}
        {card.stage === 'done' && card.cycle_seconds != null ? (
          <span>took {formatDuration(card.cycle_seconds)}</span>
        ) : (
          card.claimed_at !== '' && <span>claimed {relativeTime(card.claimed_at)}</span>
        )}
        <span>{relativeTime(card.updated_at)}</span>
      </div>
    </Link>
  )
}
